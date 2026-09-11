# Wallet Service

> A concurrency-safe peer-to-peer wallet transfer API built with **FastAPI**, **PostgreSQL**, and **async SQLAlchemy**.

[![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-async-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-4169E1?logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![Container ready](https://img.shields.io/badge/Container-ready-2496ED?logo=docker&logoColor=white)](https://www.docker.com/)

This service treats a money transfer as a database consistency problem first and an HTTP endpoint second. Its design preserves these properties during concurrent requests and client retries:

- A wallet can never have a negative balance.
- A completed transfer moves the requested amount exactly once.
- Total money is conserved across completed transfers.
- Retrying a request with the same idempotency key cannot charge a sender twice.
- Competing transfers take wallet locks in a consistent order.

## Contents

- [System at a glance](#system-at-a-glance)
- [Architecture](#architecture)
- [Transfer correctness](#transfer-correctness)
- [Repository layout](#repository-layout)
- [Getting started](#getting-started)
- [API reference](#api-reference)
- [Authentication and authorization](#authentication-and-authorization)
- [Concurrency burst runner](#concurrency-burst-runner)
- [Tests and quality checks](#tests-and-quality-checks)
- [Observability and operations](#observability-and-operations)
- [Configuration](#configuration)
- [Deployment notes](#deployment-notes)
- [Scope and security notes](#scope-and-security-notes)

## System at a glance

```mermaid
flowchart LR
    Client["API client or burst runner"] -->|"Bearer JWT + JSON"| API["FastAPI\nWallet Service"]
    API --> Auth["JWT authentication\nwallet ownership checks"]
    API --> Tx["Transactional transfer service"]
    Tx --> DB[("PostgreSQL 16")]
    DB --> W["wallets"]
    DB --> T["transfers"]
    DB --> L["ledger_entries"]
    API --> Metrics["/metrics\nPrometheus format"]
    API --> Logs["Structured JSON logs\ncorrelation ID"]
```

| Area | Choice | Why it matters |
| --- | --- | --- |
| HTTP API | FastAPI | Typed request validation and async endpoints. |
| Persistence | PostgreSQL 16 | Row locks, atomic updates, transactions, and constraints. |
| ORM | SQLAlchemy 2 async + asyncpg | Async I/O with explicit transaction boundaries. |
| Identity | HS256 JWT bearer token | The token `sub` is the caller's user UUID. |
| Metrics | Prometheus client | Scrapeable counters and latency histograms. |
| Local runtime | Docker Compose or Podman Compose | Reproducible API and PostgreSQL runtime. |
| Test tooling | pytest, HTTPX, Ruff | Async coverage and static checks. |

All monetary values are integer **paise** (`BIGINT`). Floating point money values are deliberately not used.

## Architecture

### Request lifecycle

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant A as FastAPI
    participant S as TransferService
    participant P as PostgreSQL

    C->>A: POST /transfers with JWT and idempotency key
    A->>A: Verify HS256 JWT and read subject
    A->>S: create(caller_user_id, request)
    S->>P: BEGIN
    S->>P: INSERT transfer ON CONFLICT DO NOTHING
    alt Existing idempotency key
        P-->>S: Existing transfer
        S->>S: Compare immutable request hash
        alt Same request
            S->>P: COMMIT
            S-->>A: Existing result (replay)
        else Different request
            S->>P: ROLLBACK
            S-->>A: 409 conflict
        end
    else New request
        S->>P: Lock source and destination by sorted UUID
        S->>S: Verify caller owns source wallet
        S->>P: Conditional debit if balance >= amount
        alt Sufficient funds
            S->>P: Credit destination
            S->>P: Insert debit and credit ledger entries
            S->>P: Mark transfer completed
        else Insufficient funds
            S->>P: Mark transfer declined
        end
        S->>P: COMMIT
        S-->>A: Durable terminal result
    end
    A-->>C: JSON response + X-Correlation-ID
```

### Data model

```mermaid
erDiagram
    WALLETS {
        uuid id PK
        uuid user_id UK
        bigint balance_paise
        timestamptz created_at
    }
    TRANSFERS {
        uuid id PK
        uuid caller_user_id
        varchar idempotency_key
        varchar request_hash
        uuid from_wallet_id FK
        uuid to_wallet_id FK
        bigint amount_paise
        enum status
        varchar decline_reason
        timestamptz created_at
        timestamptz completed_at
    }
    LEDGER_ENTRIES {
        uuid id PK
        uuid transfer_id FK
        uuid wallet_id FK
        enum entry_type
        bigint amount_paise
        timestamptz created_at
    }
    WALLETS ||--o{ TRANSFERS : "source / destination"
    TRANSFERS ||--o{ LEDGER_ENTRIES : records
    WALLETS ||--o{ LEDGER_ENTRIES : affects
```

| Table | Important rules |
| --- | --- |
| `wallets` | One wallet per `user_id`; database constraint prevents a balance below zero. |
| `transfers` | `(caller_user_id, idempotency_key)` is unique; a request hash detects unsafe reuse. |
| `ledger_entries` | Positive amounts only; unique debit/credit entry per transfer and wallet. |

Transfer foreign keys to wallets are `DEFERRABLE INITIALLY DEFERRED`. That avoids implicit foreign-key locks competing with the service's explicit deterministic lock ordering.

## Transfer correctness

### One atomic unit of work

Every new transfer runs inside one PostgreSQL transaction containing:

1. An idempotency claim.
2. Locks for both wallet rows in sorted UUID order.
3. Source-wallet ownership validation.
4. An atomic debit guarded by `balance_paise >= amount_paise`.
5. Destination credit only after the debit succeeds.
6. Matching debit and credit ledger entries for a completed transfer.
7. A durable `completed` or `declined` result and completion timestamp.

If a transaction fails, PostgreSQL rolls back all partial changes. An insufficient-funds request is a durable business result: it returns `200 OK` with `status: "declined"`, moves no money, and creates no ledger entries.

### Controls and failure modes

| Risk | Control |
| --- | --- |
| Two requests read the same available funds | `SELECT ... FOR UPDATE` serializes wallet mutation. |
| A debit makes balance negative | `UPDATE ... WHERE balance_paise >= :amount` combines check and debit atomically. |
| Transfers in opposite directions deadlock | Both wallets are locked by sorted UUID, independent of transfer direction. |
| Retry after a timeout | Unique idempotency key returns the original result, not another transfer. |
| Same key with changed payload | Canonical SHA-256 request hash returns `409 Conflict`. |
| Serialization, deadlock, or lock pressure error | SQLSTATE `40001`, `40P01`, and `55P03` retry up to three times with jitter. |
| Partial debit or credit | Debit, credit, ledger rows, and transfer status share one transaction. |

## Repository layout

```text
.
├── src/
│   ├── auth/                 # Bearer JWT dependency
│   ├── wallets/              # Wallet model, repository, service, routes
│   ├── transfers/            # Transfer + ledger models and transaction logic
│   ├── test_support/         # Local/test/demo-only funding endpoint
│   ├── config.py             # Environment-backed settings
│   ├── database.py           # Async engine and pool configuration
│   ├── main.py               # App, middleware, readiness, metrics
│   └── observability.py      # JSON logs and Prometheus metrics
├── tests/                    # Async API and concurrency tests
├── scripts/
│   ├── init_db.py            # Creates schema at application startup
│   ├── run_burst.py          # Python HTTP-only burst harness
│   └── run_burst.sh          # Bash HTTP-only burst harness
├── docker/postgres/init/     # Isolated wallet_test database creation
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── pyproject.toml
└── uv.lock
```

## Getting started

### Prerequisites

- Python 3.12 or later
- [uv](https://docs.astral.sh/uv/) for the simplest Python workflow
- Docker Desktop with `docker compose`, or Podman with `podman-compose`
- `curl`, `jq`, `openssl`, and `uuidgen` for the Bash burst runner

### 1. Clone and configure

```bash
git clone https://github.com/shaycorvo/wallet-app-fast-api.git
cd wallet-app-fast-api
cp .env.example .env
```

For local Compose, the `DATABASE_URL` shown in `.env.example` can remain unchanged: Compose overrides it for the app container. Change placeholder secrets before any non-local deployment.

### 2. Start API and PostgreSQL

With Docker:

```bash
docker compose up --build
```

With Podman:

```bash
podman-compose up --build
```

The API runs at [http://127.0.0.1:8000](http://127.0.0.1:8000). PostgreSQL is exposed to the host at `127.0.0.1:5433`.

At startup, `python -m scripts.init_db` creates the tables. On a fresh local database volume, the PostgreSQL initialization script also creates the isolated `wallet_test` database.

### 3. Verify the running service

Interactive API documentation is available at [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs).

```bash
curl -i http://127.0.0.1:8000/live
curl -i http://127.0.0.1:8000/ready
curl -s http://127.0.0.1:8000/metrics | head
```

Expected readiness response:

```json
{"status":"ok"}
```

### 4. Stop or reset

```bash
# Stop containers and retain local data.
docker compose down

# Stop containers and delete the local database volume.
docker compose down -v
```

For Podman, replace `docker compose` with `podman-compose`.

### Run the API from the host

Keep Compose PostgreSQL running, then start only the API process locally:

```bash
uv sync --all-groups
export DATABASE_URL='postgresql+asyncpg://wallet:wallet@localhost:5433/wallet'
uv run python -m scripts.init_db
uv run uvicorn src.main:app --reload --host 127.0.0.1 --port 8000
```

## API reference

Every public wallet and transfer route requires:

```http
Authorization: Bearer <HS256 JWT>
Content-Type: application/json
```

The token must have a UUID `sub` claim:

```json
{"sub":"6a898bd3-13b0-41e2-97e8-008051b8cb2b"}
```

The service deliberately does not implement login, registration, or token issuing. A trusted identity provider issues JWTs in a production integration; this service verifies them and applies ownership checks.

### `POST /wallets`

Gets or creates the single wallet for the authenticated user. It is safe to repeat concurrently.

```bash
curl -sS -X POST http://127.0.0.1:8000/wallets \
  -H "Authorization: Bearer $TOKEN"
```

```json
{
  "id":"7e4f1268-9821-4b8e-a718-464965638b5c",
  "balance_paise":0,
  "created_at":"2026-09-11T10:00:00.000000Z"
}
```

### `GET /wallets/{wallet_id}`

Returns only the authenticated caller's wallet. A different caller receives `403`.

```bash
curl -sS http://127.0.0.1:8000/wallets/$WALLET_ID \
  -H "Authorization: Bearer $TOKEN"
```

### `POST /transfers`

Creates a transfer, or returns the prior result for an identical request that reuses the same idempotency key.

```bash
curl -sS -X POST http://127.0.0.1:8000/transfers \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  --data "{
    \"from\": \"$SOURCE_WALLET_ID\",
    \"to\": \"$DESTINATION_WALLET_ID\",
    \"amount_paise\": 750,
    \"idempotency_key\": \"payment-2026-09-11-001\"
  }"
```

```json
{
  "id":"c3bfb10d-ee37-4717-a7b7-5ec172adb632",
  "from_wallet_id":"2b67654f-25a0-4339-bf12-431168f8db45",
  "to_wallet_id":"59182b7b-fb4a-4fe4-8ac7-4c24f62ae18f",
  "amount_paise":750,
  "status":"completed",
  "decline_reason":null,
  "created_at":"2026-09-11T10:01:00.000000Z",
  "completed_at":"2026-09-11T10:01:00.010000Z"
}
```

### `GET /transfers/{transfer_id}`

Returns a transfer only to the original caller represented by the JWT `sub`.

```bash
curl -sS http://127.0.0.1:8000/transfers/$TRANSFER_ID \
  -H "Authorization: Bearer $TOKEN"
```

| Endpoint | Success | Important errors |
| --- | --- | --- |
| `POST /wallets` | `200` wallet | `401` missing/invalid token |
| `GET /wallets/{id}` | `200` wallet | `401`, `403`, `404` |
| `POST /transfers` | `200` completed, declined, or replay | `401`, `403`, `404`, `409`, `422` |
| `GET /transfers/{id}` | `200` transfer | `401`, `403`, `404` |
| `GET /live` | `200` process liveness | Does not query database |
| `GET /ready`, `/health` | `200` database is queryable | `503` when database is unavailable |
| `GET /metrics` | `200` Prometheus exposition | No authentication in this exercise |

## Authentication and authorization

```mermaid
flowchart TD
    Token["Bearer JWT"] --> Verify["Verify HS256 signature with JWT_SECRET"]
    Verify --> Sub["Read UUID sub claim"]
    Sub --> Wallet["Check wallet user_id equals sub"]
    Wallet -->|"POST /wallets"| Create["Get or create caller wallet"]
    Wallet -->|"GET /wallets/{id}"| Read["Read only own wallet"]
    Wallet -->|"POST /transfers"| Source["Source wallet must be caller-owned"]
    Source --> Target["Destination may belong to another user"]
```

The JWT UUID is a **user identity**, not a wallet ID. The service looks up each wallet and compares its stored `user_id` to the verified token subject. A caller cannot gain access by supplying another user's wallet UUID.

## Concurrency burst runner

[`scripts/run_burst.sh`](scripts/run_burst.sh) calls the HTTP API only; it never accesses PostgreSQL directly. Therefore it can exercise local Compose, a deployed demo, or another test environment using the same behavioral checks.

| Test case | Workload | Expected result |
| --- | ---: | --- |
| Concurrent wallet creation | 50 simultaneous `POST /wallets` calls for one user | All responses return one wallet ID. |
| Idempotent retry storm | 30 simultaneous identical transfer calls | One transfer ID and exactly one 750-paise movement. |
| Bidirectional contention | 200 unique transfers across six wallets in batches of 12 | Terminal outcomes only, total remains 30,000 paise, no negative balance. |

### Run locally

Start the API, then run:

```bash
bash scripts/run_burst.sh
```

It uses these local defaults:

```text
WALLET_BASE_URL=http://127.0.0.1:8000
JWT_SECRET=local-development-only-secret-change-me
TEST_SEED_KEY=local-test-seed-key-change-me
```

Those values must match `.env`. The script creates temporary UUID users and signed test JWTs, then uses the restricted test-funding endpoint to seed balances.

### Run against a demo deployment

```bash
WALLET_BASE_URL='https://your-wallet-service.example.com' \
JWT_SECRET='the-demo-jwt-secret' \
TEST_SEED_KEY='the-demo-test-seed-key' \
bash scripts/run_burst.sh
```

The script begins with a `GET /ready` preflight, emits concise stage-level progress, exits nonzero on a failed condition, and does not print bearer tokens or seed secrets.

### Test funding endpoint

The runner calls:

```http
POST /internal/test/wallets/{wallet_id}/fund
X-Test-Seed-Key: <TEST_SEED_KEY>
```

This endpoint is omitted from OpenAPI and is only active when `APP_ENVIRONMENT` is `local`, `test`, or `demo` and a `TEST_SEED_KEY` exists. In `production`, it returns `404` and must remain inaccessible.

## Tests and quality checks

Tests use the dedicated `wallet_test` database. They drop and recreate only that database's tables, never the live `wallet` database used by the application.

```bash
# Install locked application and development dependencies.
uv sync --all-groups

# Run all asynchronous tests.
uv run pytest -q

# Run static checks.
uv run ruff check .
```

Current test coverage includes concurrent wallet creation, idempotency storms and conflicts, insufficient-funds results without partial movement, bidirectional contention with conservation checks, ownership authorization, strict transfer request validation, probes, and route-template metrics. The expected full-suite result is `13 passed`.

## Observability and operations

### Probes

| Endpoint | Meaning | Intended use |
| --- | --- | --- |
| `/live` | Process can respond; no database query. | Container liveness and restart decisions. |
| `/ready` | `SELECT 1` against PostgreSQL succeeded. | Load balancer readiness and deploy checks. |
| `/health` | Alias for `/ready`. | Simple external health monitor. |

The container has a `/live` health check and the application disposes the database engine during shutdown.

### JSON logs and correlation IDs

Logs are structured JSON. Requests are given a UUID correlation ID unless the client supplies one:

```bash
curl -i http://127.0.0.1:8000/live \
  -H 'X-Correlation-ID: interview-demo-001'
```

The returned `X-Correlation-ID` connects the client operation to events such as:

```json
{"level":"info","event":"transfer_created","correlation_id":"interview-demo-001","transfer_id":"...","status":"completed","amount_paise":750}
```

### Prometheus metrics

| Metric | Labels | Meaning |
| --- | --- | --- |
| `wallet_http_requests_total` | method, path, status | Request volume by matched route template. |
| `wallet_http_request_duration_seconds` | method, path | Request latency histogram. |
| `wallet_transfers_total` | status | New transfers by terminal status. |
| `wallet_transfer_creations_total` | none | New transfer requests, excluding replays. |
| `wallet_transfers_declined_total` | reason | Declined transfer outcomes. |
| `wallet_idempotent_replays_total` | none | Requests served from an existing result. |

Metrics use templates such as `/wallets/{wallet_id}`, not concrete UUID paths, preventing unbounded label cardinality.

### Resilience settings

The async engine provides a configurable pool, `pool_pre_ping=True`, a 2-second pool checkout timeout, a 5-second PostgreSQL statement timeout, and a 2-second lock timeout. Database availability or lock-pressure failures at the HTTP boundary return sanitized `503 Service Unavailable` responses with `Retry-After: 1`; unknown failures return a sanitized `500`.

## Configuration

Copy [`.env.example`](.env.example) to `.env`. `.env` is ignored by Git and must not be committed.

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `APP_ENVIRONMENT` | No | `local` | `local`, `test`, and `demo` can enable protected test funding. |
| `DATABASE_URL` | Yes | none | Async SQLAlchemy PostgreSQL URL. |
| `TEST_DATABASE_URL` | No | derived as `.../wallet_test` | Isolated database used by pytest. |
| `DATABASE_POOL_SIZE` | No | `10` | Persistent async pool size. |
| `DATABASE_MAX_OVERFLOW` | No | `5` | Extra temporary pool connections. |
| `JWT_SECRET` | Yes | none | HS256 verification secret; use a long random value outside local work. |
| `TEST_SEED_KEY` | No | none | Key required for local/test/demo funding. |

Generate a strong deployment secret locally, never in source code:

```bash
openssl rand -hex 32
```

## Deployment notes

The multi-stage image is based on `python:3.12-slim`, exposes port `8000`, and runs as a non-root `app` user. Deploy the image alongside a **managed PostgreSQL** database and set at least:

```text
APP_ENVIRONMENT=demo
DATABASE_URL=postgresql+asyncpg://...
JWT_SECRET=<long random value>
TEST_SEED_KEY=<different long random value>
DATABASE_POOL_SIZE=3
DATABASE_MAX_OVERFLOW=2
```

The smaller pool is appropriate for an entry-level managed database and avoids exhausting its available connections. Use `APP_ENVIRONMENT=demo` only for a controlled public test demonstration. After running the burst proof, set `APP_ENVIRONMENT=production` or tear down the demo so the funding endpoint cannot be called.

Before a production deployment, add managed secret storage and rotation, TLS termination, restricted metrics access, backups, migrations, rate limits, JWT issuer/audience validation, alerting, and a formal audit/retention policy.

## Scope and security notes

This is a focused wallet-transfer exercise. It intentionally does not implement account signup, a user table, login/token issuance, refunds, reversals, payouts, or settlement.

Within its scope, it provides:

- HS256 bearer JWT signature verification.
- Wallet and transfer-read authorization based on JWT subject.
- Source-wallet authorization before transfer mutation.
- Strict transfer schema validation with unexpected fields rejected.
- Positive integer-only money amounts with a maximum bound.
- Environment-based secrets and a non-root container runtime.
- Sanitized server/database error responses.
- Environment-gated funding protected with constant-time secret comparison.

## Useful commands

```bash
# Start local API and PostgreSQL.
docker compose up --build

# Follow application logs.
docker compose logs -f app

# Run automated checks.
uv run pytest -q
uv run ruff check .

# Run the HTTP concurrency proof.
bash scripts/run_burst.sh

# Inspect project metrics.
curl -s http://127.0.0.1:8000/metrics | grep '^wallet_'

# Stop services while retaining the local database.
docker compose down
```

---

Built to demonstrate that money invariants survive concurrent HTTP requests, retries, and insufficient-funds outcomes.