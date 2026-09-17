# Warp Engine

Auto-discovery REST CRUD API generator with LLM-powered database catalog intelligence.

Warp connects to your database, discovers tables and columns, and generates a fully functional REST API with filtering, sorting, pagination, and authentication. Its catalog intelligence layer uses LLMs to generate human-readable descriptions, semantic types, and multi-language documentation for your entire schema — then injects all of it into the OpenAPI spec so downstream LLMs and tools get rich context automatically.

## Features

- **Auto-discovery** - Automatically discovers database tables and generates API endpoints
- **Full CRUD** - Create, Read, Update, Delete operations for all tables
- **Filtering** - Filter results using query parameters with operators (eq, gt, lt, in, like, is_null, etc.)
- **Sorting** - Sort results by any column with ascending/descending support
- **Pagination** - Built-in pagination with configurable limits
- **Raw Queries** - Secure raw SQL execution with command whitelist
- **Typed Schema** - `GET /schema` reports column types, primary keys and planner row estimates (no `COUNT(*)`)
- **Streaming Export** - `GET|POST /{table}/export` streams rows from a server-side cursor as JSON, NDJSON or Arrow IPC
- **Multi-DB Support** - PostgreSQL and MySQL
- **Authentication** - API key authentication with role-based access control
- **Catalog Intelligence** - LLM-powered schema analysis with semantic types and descriptions
- **Multi-Language** - Catalog descriptions in multiple languages with auto-translation
- **Human-in-the-Loop Review** - Interactive draft/review/approve workflow for catalog edits
- **Override Persistence** - User edits survive LLM regeneration via `user_overrides`
- **OpenAPI Enrichment** - Auto-enrich OpenAPI specs with catalog metadata and `x-llm-context` extensions (example values opt-in)
- **Export** - Export catalogs to JSON, YAML, or Markdown
- **Production Ready** - Connection retry, health checks, structured logging, Docker support

## Quick Start

### Using Docker Compose

```bash
git clone https://github.com/yasinyaman/warp.git
cd warp

# Start all services (API + PostgreSQL + MySQL + Adminer)
docker-compose up -d

# API: http://localhost:8000
# Docs: http://localhost:8000/docs
# Adminer: http://localhost:8080
```

### Using pip

```bash
pip install -e ".[dev,llm]"
```

#### Reproducible installs (pinned + hashed)

`pyproject.toml` keeps flexible `>=` ranges for library consumers. For
reproducible environments, two hash-pinned lock files are committed:
`requirements.lock` (all extras; CI and local dev) and `requirements-prod.lock`
(runtime + `llm` only; the Docker image). Install from them with
[uv](https://docs.astral.sh/uv/) and regenerate with `make lock`:

```bash
uv pip sync --require-hashes requirements.lock   # or: pip install --require-hashes -r requirements.lock
uv pip install --no-deps -e .
```

```bash

# Set environment variables
export DB_HOST=localhost
export DB_NAME=mydb
export DB_USER=postgres
export DB_PASS=secret

# Run the API server
warp

# Or use the catalog CLI
warp-catalog analyze -d primary_db --auto-approve
```

## Configuration

Create a `config/database.yaml` file:

```yaml
databases:
  - name: primary_db
    type: postgresql
    host: ${DB_HOST:localhost}
    port: ${DB_PORT:5432}
    database: ${DB_NAME:myapp}
    username: ${DB_USER:postgres}
    password: ${DB_PASS:}
    options:
      pool_size: 10
      ssl: false

settings:
  auto_discover_tables: true
  excluded_tables:
    - migrations
    - alembic_version
  pagination:
    default_limit: 50
    max_limit: 1000
  export:
    enabled: true           # GET|POST /{table}/export
    max_rows: 0             # 0 = unlimited; a cap is announced via X-Export-Max-Rows
    batch_size: 5000        # rows per cursor fetch / response chunk
    statement_timeout_ms: 0 # per-export statement timeout (0 = driver default)
  enable_raw_query: false   # opt-in only; refused at startup in production
  api_prefix: /api/v1

  # Catalog Intelligence
  catalog:
    storage_path: "./catalogs"
    default_format: "json"
    auto_cross_reference: true
    auto_enrich_openapi: true
    openapi_enrichment_lang: "en"

  # LLM Provider
  llm:
    provider: "${LLM_PROVIDER:ollama}"   # openai, anthropic, gemini, ollama
    model: "${LLM_MODEL:gemma3:1b}"
    api_key: "${LLM_API_KEY:}"
    base_url: "${LLM_BASE_URL:http://localhost:11434}"
    temperature: 0.3
    max_tokens: 4096

  # Internationalization
  i18n:
    default_language: "en"
    languages: ["en"]
    auto_translate: true
    translation_strategy: "single"

  # Authentication
  auth:
    enabled: false
    header_name: X-API-Key
    public_paths:
      - /health
      - /docs
      - /redoc
      - /openapi.json
```

### LLM Provider Configuration

Warp supports multiple LLM providers. The API key can be set in config or via environment variables:

| Provider | Config `provider` | Environment Variable | Notes |
|----------|------------------|---------------------|-------|
| OpenAI | `openai` | `OPENAI_API_KEY` | GPT-4o, GPT-4o-mini |
| Anthropic | `anthropic` | `ANTHROPIC_API_KEY` | Claude models |
| Google Gemini | `gemini` | `GOOGLE_API_KEY` | Gemini models |
| Ollama | `ollama` | (none needed) | Local models, no API key required |

**Using Ollama (local, free):**

```bash
# Install and start Ollama
ollama serve

# Pull a model
ollama pull gemma3:1b

# Set in config or env
export LLM_PROVIDER=ollama
export LLM_MODEL=gemma3:1b
export LLM_BASE_URL=http://localhost:11434
```

**Docker + Ollama:** When running Warp in Docker with Ollama on the host, Warp automatically rewrites `localhost` to `host.docker.internal`. The `docker-compose.yml` includes `extra_hosts` for Linux compatibility.

## REST API

### CRUD Endpoints

Warp auto-generates these for each discovered table:

```bash
GET    /api/v1/{table}        # List records (paginated, filtered, sorted)
GET    /api/v1/{table}/{id}   # Get single record
POST   /api/v1/{table}        # Create record
PUT    /api/v1/{table}/{id}   # Update record
PATCH  /api/v1/{table}/{id}   # Partial update record
DELETE /api/v1/{table}/{id}   # Delete record
```

Every table is always reachable under `/api/v1/{db_name}/{table}`. With a
single configured database the bare `/api/v1/{table}` form is kept as an alias
(it is not listed in the OpenAPI document). `GET /info` reports the effective
`api_prefix` and a `capabilities` block (schema, export formats, raw query,
filter operators) so clients can adapt to a running instance.

### Filtering

```bash
# Exact match
GET /api/v1/users?filter[status]=active

# Comparison operators
GET /api/v1/products?filter[price][gte]=100&filter[price][lte]=500

# IN operator
GET /api/v1/orders?filter[status][in]=pending,processing

# NULL check
GET /api/v1/users?filter[deleted_at][is_null]=true

# LIKE operator
GET /api/v1/users?filter[name][like]=%john%
```

### Sorting

```bash
GET /api/v1/users?sort=name:asc
GET /api/v1/products?sort=price:desc
GET /api/v1/orders?sort=status:asc,created_at:desc
GET /api/v1/users?sort=-created_at         # prefix shorthand
```

### Pagination

```bash
GET /api/v1/users?limit=20&offset=0
GET /api/v1/users?limit=20&offset=40       # page 3
```

### Raw Queries

```bash
POST /api/v1/query/execute
Content-Type: application/json

{
  "query": "SELECT * FROM users WHERE status = :status LIMIT 10",
  "params": {"status": "active"}
}
```

Placeholders are `:name` and `params` is an object; the same name may be used
more than once, `::type` casts are left alone, and every referenced name must
be supplied (a typo is a 400, not a silent no-op).

```
```

### Schema

```bash
GET /api/v1/schema                 # every table: typed columns, primary key, row estimate
GET /api/v1/{table}/schema         # one table
GET /api/v1/schema?estimates=false # skip the planner statistics
```

Each column carries the database type, a coarse `kind`
(`int`/`float`/`bool`/`str`/`json`/`bytes`/`list`/`datetime`/`date`/`time`/`uuid`),
nullability, precision/scale, max length and default. `row_estimate` comes from
planner statistics (`pg_class.reltuples`, `information_schema.TABLES`) and is
`null` when the table was never analyzed.

### Export (streaming)

```bash
# Same fields / filter / sort parameters as the list endpoint, no pagination
GET /api/v1/orders/export?fields=id,total&filter[status][eq]=paid&sort=id:asc
GET /api/v1/orders/export?format=ndjson
GET /api/v1/orders/export?format=arrow          # needs: pip install "warp-engine[arrow]"

# JSON body for long `in` lists
POST /api/v1/orders/export
{"fields": ["id", "total"],
 "filters": [{"column": "customer_id", "op": "in", "value": [1, 2, 3]}],
 "sort": [{"column": "id", "direction": "asc"}],
 "limit": 100000, "format": "arrow"}
```

Rows are pulled through a server-side cursor in `export.batch_size` chunks and
written to the response as they arrive, so memory stays flat however large the
table is. `json` produces `{"items": [...], "row_count": N}` (decimals as
strings, bytes as base64), `ndjson` one object per line, `arrow` an Arrow IPC
stream with typed columns (`decimal128`, `timestamp[us, UTC]`, `binary`, …).
Responses carry `X-Export-Format` and `Cache-Control: no-store`; when
`export.max_rows` cuts the result, `X-Export-Max-Rows` says so. `format=arrow`
without `pyarrow` answers 501; `export.enabled: false` answers 403.

## Catalog Intelligence

Warp includes an LLM-powered catalog system that analyzes your database schema and generates rich metadata: descriptions, semantic types, tags, relationships, example values, and multi-language documentation.

### Analyze via API

```bash
# Analyze all tables and auto-approve
curl -X POST http://localhost:8000/api/v1/catalog/analyze \
  -H "Content-Type: application/json" \
  -d '{"database": "primary_db", "auto_approve": true}'

# Analyze specific tables
curl -X POST http://localhost:8000/api/v1/catalog/analyze \
  -H "Content-Type: application/json" \
  -d '{"database": "primary_db", "tables": ["users", "orders"], "auto_approve": true}'
```

### CLI Commands

```bash
# Analyze database and generate catalog (draft)
warp-catalog analyze -d primary_db

# Analyze with auto-approve (skip review)
warp-catalog analyze -d primary_db --auto-approve

# Analyze specific tables
warp-catalog analyze -d primary_db -t users,orders --auto-approve

# Interactive review: table-by-table approve/edit/skip
warp-catalog review -d primary_db --lang en

# List available catalogs
warp-catalog list

# Show catalog details
warp-catalog info -d primary_db

# Export catalog
warp-catalog export -d primary_db -f markdown -o catalog.md

# Enrich OpenAPI spec with catalog descriptions
warp-catalog enrich-openapi -d primary_db -i openapi.json -o enriched.json   # add --include-examples for sample values

# Full pipeline: DB -> Catalog -> Export -> Enrich OpenAPI
warp-catalog pipeline -d primary_db -f json -o catalog.json --openapi openapi.json
```

### Catalog REST API

All endpoints are under `/api/v1/catalog`:

```bash
# List catalogs
GET /catalog

# Analyze database
POST /catalog/analyze
{ "database": "primary_db", "auto_approve": true }

# View catalog info
GET /catalog/{db}

# View draft with review status
GET /catalog/{db}/draft

# View single table detail (columns, relationships, sample values)
GET /catalog/{db}/draft/tables/{table}

# Edit table fields (description, human_name, tags, relationships)
PATCH /catalog/{db}/draft/tables/{table}
{ "description": {"en": "Updated description"}, "tags": ["core"] }

# Edit column fields (description, semantic_type, tags)
PATCH /catalog/{db}/draft/tables/{table}/columns/{column}
{ "semantic_type": "email", "tags": ["pii"] }

# Approve all tables
POST /catalog/{db}/approve

# Approve single table
POST /catalog/{db}/approve/{table}

# Export catalog
GET /catalog/{db}/export?format=json&lang=en

# Delete catalog
DELETE /catalog/{db}
```

### Draft/Review/Approve Workflow

1. **Analyze** generates a `draft` catalog with all tables in `pending` status
2. **Review** each table: view descriptions, edit fields, approve or skip
3. **Approve** transitions the catalog to `approved` status
4. OpenAPI enrichment triggers automatically after approval

### User Override Persistence

When users edit catalog fields (description, human_name, semantic_type, tags, etc.), edits are stored in a separate `user_overrides` field. On re-analysis:

1. Existing overrides are extracted before LLM regeneration
2. LLM generates fresh descriptions from the current schema
3. User overrides are re-applied on top of new LLM output
4. Edited tables are marked as `modified`

This ensures user edits survive across schema changes and LLM re-analysis.

## OpenAPI Enrichment

After catalog analysis and approval, Warp automatically enriches the OpenAPI spec (`/openapi.json`) with all catalog metadata. This makes the API self-documenting for both humans and LLMs. Only **approved** catalogs are published; the context is refreshed after analyze/approve/delete, and several databases are merged into one `x-llm-context` (`{"databases": [...]}`).

Real sample values are **not** written into the spec unless `catalog.openapi_include_examples: true` (they may contain personal data), and `/openapi.json` is served through the auth manager — see the production checklist below.

### What Gets Enriched

**Operation level** (each endpoint):
- Table description, human name, tags, approximate row count
- Relationships with types (many-to-one, one-to-many, etc.)
- Full column listing with types, constraints, and descriptions (on GET list endpoints)
- `x-llm-context` extension with structured machine-readable metadata

**Schema level** (component models):
- Table description prepended to schema description
- Column descriptions with semantic types, constraints, and example values
- `x-llm-context` extension per schema and per property
- Real `example` values from database samples

**Top-level `x-llm-context`** extension:
- Complete database overview: all tables, columns, relationships, types, and descriptions
- Designed for LLMs to consume the entire schema context in one read

### Example: `x-llm-context` in OpenAPI

```json
{
  "x-llm-context": {
    "database": "primary_db",
    "database_type": "postgresql",
    "table_count": 5,
    "tables": [
      {
        "name": "categories",
        "human_name": "Product Categories",
        "description": "Stores product categories...",
        "tags": ["categories", "products"],
        "row_count": 8,
        "columns": [
          {
            "name": "id",
            "type": "integer",
            "description": "Unique identifier",
            "is_primary_key": true,
            "nullable": false,
            "examples": [1, 2, 3]
          },
          {
            "name": "parent_id",
            "type": "integer",
            "description": "Links to parent category",
            "foreign_key": "categories.id",
            "nullable": true
          }
        ],
        "relationships": [
          {"from": "parent_id", "to": "categories.id", "type": "many-to-one"}
        ]
      }
    ]
  }
}
```

### LLM Resilience

Warp is designed to work with small local models (e.g., `gemma3:1b` via Ollama):

- **Fallback**: If the LLM fails (quota, connection, bad response), tables are created with schema-only info (no descriptions) instead of failing the entire analysis
- **Flexible JSON parsing**: Small models may return simplified JSON structures — Warp normalizes flat strings, lists, and dicts into the expected catalog format
- **Partial success**: If some tables fail and others succeed, the successful ones are kept and a warning is logged
- **Clear error messages**: Quota errors, connection failures, and missing models produce actionable messages with fix instructions

## Health Endpoints

```bash
GET /health    # Full health check with database status
GET /ready     # Kubernetes readiness probe
GET /live      # Kubernetes liveness probe
GET /info      # API information and discovered tables
```

## Project Structure

Hexagonal (ports & adapters) layout — dependencies point inward and are
enforced by import-linter (see [ADR-0007](docs/adr/0007-hexagonal-architecture.md)):

```
src/warp/
├── main.py / cli.py            # entry points: build the Container, start uvicorn / Click
├── infrastructure/             # composition root (bootstrap), config loader, logging
├── adapters/
│   ├── inbound/http/           # FastAPI app + lifespan, auth, routes/{crud,catalog,query}
│   ├── inbound/cli/            # warp-catalog commands
│   └── outbound/
│       ├── db/                 # PostgreSQL/MySQL gateways, SafeQueryBuilder, identifiers,
│       │                       #   named params, comment/sample readers
│       ├── llm/                # OpenAI/Anthropic/Gemini/Ollama providers + LLMClient
│       ├── catalog_store/      # file-based CatalogRepository
│       └── export/             # JSON/YAML/Markdown exporters
├── application/
│   ├── ports/                  # DatabaseGateway, CatalogRepository, TextGenerator, ...
│   ├── services/               # crud, schema_discovery, catalog_analysis, catalog_review,
│   │                           #   cross_reference, catalog_export, openapi_enrichment, pipeline
│   ├── config.py               # Settings models, RuntimeEnv, production validation
│   └── container.py            # the wired object graph handed to inbound adapters
└── domain/                     # schema/catalog entities, review transitions, naming,
                                #   samples + PII policy, filtering/sorting/pagination, errors
config/database.yaml            # main configuration (env-interpolated)
docker-compose.yml, Dockerfile  # dev stack / production image (installs requirements-prod.lock)
tests/unit/{domain,application,adapters,infrastructure}
```

## Development

```bash
# Clone repository
git clone https://github.com/yasinyaman/warp.git
cd warp

# Create virtual environment
python -m venv .venv
source .venv/bin/activate

# Install with dev and LLM dependencies
pip install -e ".[dev,llm]"

# Run tests
pytest

# Run with hot-reload
APP_ENV=development python -m warp.main

# Docker development
docker-compose up -d --build
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `APP_ENV` | `development` | Environment (development/production) |
| `API_HOST` | `0.0.0.0` | Host to bind |
| `API_PORT` | `8000` | Port to bind |
| `API_WORKERS` | `1` | Number of workers |
| `LOG_LEVEL` | `INFO` | Log level (DEBUG, INFO, WARNING, ERROR) |
| `LOG_FORMAT` | `colored` | Log format (colored/json) |
| `CONFIG_PATH` | `config/database.yaml` | Configuration file path |
| `CORS_ORIGINS` | `*` | Allowed CORS origins |
| `DB_HOST` | `localhost` | PostgreSQL host |
| `DB_PORT` | `5432` | PostgreSQL port |
| `DB_NAME` | - | PostgreSQL database name |
| `DB_USER` | - | PostgreSQL user |
| `DB_PASS` | - | PostgreSQL password |
| `MYSQL_HOST` | `localhost` | MySQL host |
| `MYSQL_PORT` | `3306` | MySQL port |
| `MYSQL_DB` | - | MySQL database name |
| `MYSQL_USER` | - | MySQL user |
| `MYSQL_PASS` | - | MySQL password |
| `LLM_PROVIDER` | `ollama` | LLM provider (openai, anthropic, gemini, ollama) |
| `LLM_MODEL` | `gemma3:1b` | LLM model name |
| `LLM_API_KEY` | - | LLM API key (not needed for Ollama) |
| `LLM_BASE_URL` | `http://localhost:11434` | LLM base URL (auto-rewrites for Docker) |
| `OPENAI_API_KEY` | - | OpenAI API key (fallback if LLM_API_KEY not set) |
| `ANTHROPIC_API_KEY` | - | Anthropic API key (fallback if LLM_API_KEY not set) |
| `GOOGLE_API_KEY` | - | Google/Gemini API key (fallback if LLM_API_KEY not set) |

## Production Deployment (secure defaults)

Warp fails safe: when `APP_ENV=production` the app **refuses to start** with
unsafe configuration. Production checklist:

1. **Secrets via environment, never in the repo.** Copy `.env.example` → `.env`
   (gitignored) and set strong values. Never commit real credentials/keys.
2. **`APP_ENV=production`** — disables `/docs` & `/redoc`, switches logs to JSON,
   and enables the startup safety checks below.
3. **Authentication on** — `settings.auth.enabled: true` with real API keys via
   `API_KEY_*` env vars. Startup is refused if auth is off in production.
   Catalog endpoints require `read` (views), `create` (analysis), `update`
   (edits/approvals) or `delete`.
4. **Explicit CORS allowlist** — `CORS_ORIGINS=https://app.example.com,...`.
   `*` is rejected in production (credentials are only sent with an explicit
   allowlist).
5. **Raw SQL endpoint off** — `enable_raw_query: false` (default). Startup is
   refused if enabled in production; if you must enable it, configure a
   **read-only** DB role via `databases[].readonly_username`/`readonly_password`
   (env `DB_READONLY_USER`/`DB_READONLY_PASS`) so a whitelist bypass cannot write.
6. **Run the production image** — the `Dockerfile` is multi-stage, runs as a
   non-root user, ships no dev dependencies, and has no `--reload`. Mount a
   hardened `config/database.yaml` and pass secrets via the environment.
7. **Reproducible installs** — `uv pip sync --require-hashes requirements.lock`
   (the Docker image installs `requirements-prod.lock`).
8. **Least-privilege DB account** — grant only what the API needs; restrict
   write access to tables that should be writable.
9. **Keep `/openapi.json` private** — remove it from `auth.public_paths` (the
   enriched spec carries catalog descriptions and `x-llm-context`); startup is
   refused in production while it is public unless
   `auth.allow_public_openapi: true`. Leave `catalog.openapi_include_examples`
   off unless real row values may be shown.

Parameterized queries, mass-assignment protection, timing-safe API-key checks,
validated catalog names and PII-free stored catalogs are on by default — see
[SECURITY.md](SECURITY.md). To contribute, see [CONTRIBUTING.md](CONTRIBUTING.md).

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE) for details.
