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
- **Multi-DB Support** - PostgreSQL, MySQL, SQL Server and Oracle (plus any ODBC data source, best effort)
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

# Also start SQL Server (profile `mssql`; set MSSQL_PASS in .env first)
docker compose --profile mssql up -d

# Or Oracle (profile `oracle`; set ORACLE_PASS in .env first)
docker compose --profile oracle up -d

# API: http://localhost:8000
# Docs: http://localhost:8000/docs
# Adminer: http://localhost:8080
```

### Using pip

```bash
pip install -e ".[dev,llm,odbc]"   # drop `odbc` if you do not need SQL Server / Oracle / ODBC
```

#### Reproducible installs (pinned + hashed)

`pyproject.toml` keeps flexible `>=` ranges for library consumers. For
reproducible environments, two hash-pinned lock files are committed:
`requirements.lock` (all extras; CI and local dev) and `requirements-prod.lock`
(runtime + `llm` + `odbc`; the Docker image). Install from them with
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

### SQL Server and other ODBC sources

`type: mssql` (or `sqlserver`) connects through ODBC with the SQL Server
profile: full schema discovery (identity/computed columns, keys, indexes),
`OUTPUT`-based writes, `TOP` / `OFFSET … FETCH` pagination, comments from
`MS_Description` extended properties and row counts for the catalog.

```yaml
databases:
  - name: mssql_db
    type: mssql
    host: ${MSSQL_HOST:localhost}
    port: ${MSSQL_PORT:1433}          # SQL Server's default; set it explicitly
    database: ${MSSQL_DB:master}
    username: ${MSSQL_USER:sa}
    password: ${MSSQL_PASS:}
    options:
      schema: dbo                     # schema to introspect (default: dbo)
      driver: ODBC Driver 18 for SQL Server   # default
      encrypt: true                   # Driver 18 default; `strict`/`optional` also accepted
      trust_server_certificate: false # true only for self-signed dev servers
      # extra: "ApplicationIntent=ReadOnly"   # raw Key=Value pairs appended verbatim
      # connection_string: "DSN=warp"         # use an ODBC DSN instead of host/port/...
```

Requirements: the `odbc` extra (`pip install "warp-engine[odbc]"`, included in
both lock files) and Microsoft's ODBC driver on the machine — the Docker image
ships it (opt out with `docker build --build-arg WITH_MSSQL_ODBC=0 .`), CI
installs it, locally use `brew install unixodbc && brew tap microsoft/mssql-release && brew install msodbcsql18`
on macOS or the `msodbcsql18` package from
[packages.microsoft.com](https://learn.microsoft.com/sql/connect/odbc/linux-mac/installing-the-microsoft-odbc-driver-for-sql-server)
on Linux.

### Row-level security

An API key may carry mandatory row conditions. They are ANDed onto every read
of that table and enforced on writes; a caller cannot widen, drop or override
them with query parameters.

```yaml
auth:
  enabled: true
  api_keys:
    - key: ${ACME_KEY}
      name: acme
      tenant: acme
      permissions: [read, create, update, delete]
      row_filters:
        orders:
          - column: tenant_id
            value: ${tenant}        # or ${username}, or a literal
        audit_log:
          - column: severity
            operator: in
            value: [info, warning]
```

What this guarantees, and what it does not:

- **List, export and stream** push the conditions into the `WHERE` clause, so
  the database never returns a row the caller may not see.
- **Reads and writes by primary key** have no `WHERE` to push into, so the row
  is matched in memory. A row outside the policy is reported as **404, not
  403** — saying "this exists but is not yours" is itself a disclosure.
- **Writes cannot escape the scope.** Creating a row into another tenant, or
  moving one there with an update, is a 400. Omitting the scope column on
  create is refused too, so a database default cannot decide it.
- **Raw SQL is denied** to any key with row rules, even one holding `all`:
  conditions cannot be pushed into a statement the caller wrote, and one
  `SELECT` would make the rules irrelevant.
- **A restricted key needs authentication to mean anything.** With
  `auth.enabled: false` nobody is identified and the rules apply to nothing;
  in production that combination refuses startup. The same applies to a data
  path listed in `auth.public_paths` — do not put one there.

### Column masking

Masking rules attach to the catalog's **semantic types**, not to column names.
That is the whole point: when the catalog labels a newly discovered
`contact_email` as `email`, it is masked from that moment, instead of leaking
until somebody remembers to write a rule for it.

```yaml
masking:
  enabled: true
  rules:                    # semantic type -> strategy
    email: partial          # a***@example.com
    phone: last4            # ***6789
    address: redact         # ***
  by_role:                  # a role's rules replace `rules` entirely
    support:
      email: redact
  exempt_roles: [admin]     # sees raw values
  hash_secret: ${WARP_MASKING_HASH_SECRET}   # only needed by `hash`
```

Strategies: `partial`, `last4`, `hash`, `redact` and `null`. A `NULL` value
stays `NULL` — masking one would invent the appearance of data.

`hash` produces a **keyed pseudonym**: the same value masks the same way in
every row, so it can still be joined on, and anyone holding the key can
re-derive the original. That is pseudonymisation, not anonymisation — keep the
key apart from anything it masks. It is keyed because an unkeyed digest of
enumerable data is not a mask at all: an email falls to a wordlist and an
11-digit national id to a loop, whatever the digest length. Rotating or losing
the key breaks correlation with data exported earlier, which is the price of
stability and the right trade.

Applied to both paths that carry rows: CRUD responses, and `/export` in all
three formats (JSON, NDJSON and Arrow). `/schema` returns no sample values, and
catalog samples have their own `analysis.mask_pii_samples`.

Two things to know:

- **Masking needs an approved catalog.** Rules key on semantic types, and only
  the catalog knows which column carries which. A draft is ignored — its labels
  have not been reviewed, and masking the wrong columns is as damaging as
  masking none. With masking enabled and no approved catalog, Warp logs that
  nothing will be masked rather than failing quietly.
- **A mask has to fit the column.** The text strategies need a text column and
  `null` needs a nullable one, or the response could not carry the result.
  Anything that does not fit is reported at startup, naming the column.
- **`hash` refuses to load without a key.** Masking configured with a `hash`
  rule and no `masking.hash_secret` fails when the configuration is *read*,
  whatever the environment and whether or not table discovery is on. Neither
  way of carrying on is available: falling back to an unkeyed digest is the
  same exposure under a safer name, and skipping the rule returns readable PII
  while reporting that it was masked. In production a secret shorter than 32
  characters is refused too.

### Audit trail

One append-only event per data-touching request.

```yaml
audit:
  enabled: true
  file: /var/log/warp/audit.jsonl   # optional; events always go to `warp.audit`
```

Each line is one JSON object:

```json
{"event": "data_access", "at": "2026-09-19T09:00:00Z", "action": "read",
 "database": "shop", "table": "orders", "actor": "acme-reader",
 "tenant": "acme", "roles": ["support"], "request_id": "9f2c...",
 "status": 200, "row_count": 42, "row_filtered": true,
 "masked_columns": ["email"], "filtered_columns": ["status"],
 "restricted": true, "is_write": false}
```

`row_filtered` and `masked_columns` are the point: a log that cannot tell
"read the table" from "read their own tenant, with the email column masked"
cannot answer the only question it is ever asked afterwards.

**What it never contains:** row values, filter literals or SQL parameters.
Filter *column names* are recorded; the values are not. An audit log that
quotes the data it audits becomes a second copy of that data, in a file that
usually has weaker access controls and a longer retention than the database.

Every response carries `X-Request-ID` (a caller-supplied one is honoured), and
the same id appears on the event, so an audit line and the application logs for
the same request can be joined.

### Oracle

`type: oracle` connects through the same ODBC adapter with an Oracle profile:
schema discovery from `ALL_TAB_COLS` / `ALL_CONSTRAINTS` / `ALL_INDEXES`
(falling back to `ALL_TAB_COLUMNS` on pre-12c servers, which have no
`IDENTITY_COLUMN`), `FETCH FIRST` sampling, `OFFSET … FETCH` pagination,
comments from `ALL_TAB_COMMENTS` / `ALL_COL_COMMENTS` and row counts from
`ALL_TABLES.NUM_ROWS`.

```yaml
databases:
  - name: oracle_db
    type: oracle
    host: ${ORACLE_HOST:localhost}
    port: ${ORACLE_PORT:1521}
    database: ${ORACLE_SERVICE:FREEPDB1}   # the service name, not a schema
    username: ${ORACLE_USER:warp}
    password: ${ORACLE_PASS:}
    options:
      driver: Oracle 23 ODBC driver       # required: the installed driver's name
      # schema: HR                        # default: the connecting user
```

Two Oracle-specific behaviours worth knowing:

- **The schema is the connecting user**, not `database` (which is a service
  name). Warp resolves it with `SELECT USER FROM DUAL` unless `options.schema`
  names one.
- **Identifiers are folded to upper case** before quoting, because Oracle
  stores unquoted names that way — a table created as `users` is `USERS`, and
  that is the name Warp reports and accepts.

**One fidelity limit, and its fix.** A column declared as bare `NUMBER`, with
no precision, is handed over by the ODBC driver as an IEEE double — so an id
above 2^53 arrives already rounded, and no layer above the driver can recover
it. Warp detects these columns during introspection, marks them `inexact` in
the typed schema and warns once per table. The remedy is in the DDL:
`NUMBER(38)` makes the driver return an exact `Decimal`.

```text
WARP.accounts: ID declared as NUMBER without a precision. The ODBC driver
returns these as floating point, so values above 2^53 lose precision before
Warp sees them. Declare a precision (e.g. NUMBER(38)) to get exact values.
```

Requirements: the `odbc` extra plus Oracle's Instant Client ODBC driver
([Instant Client downloads](https://www.oracle.com/database/technologies/instant-client/downloads.html)),
registered in `odbcinst.ini`. For a local server,
`docker compose --profile oracle up -d` starts `gvenzl/oracle-free`, which —
unlike the SQL Server image — has arm64 builds and so runs natively on Apple
Silicon.

`type: odbc` is a best-effort generic profile for other ODBC data sources:
`options.driver` (or `options.connection_string`) is required, quoting is ANSI,
placeholders are `?`, schema discovery goes through the ODBC catalog functions
(identity columns are only detected when the driver reports them), written
rows are re-selected by their key, and there is no comment/row-count catalog
intelligence.

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
GET    /api/v1/{table}/{key}  # Get single record
POST   /api/v1/{table}        # Create record
PUT    /api/v1/{table}/{key}  # Update record
PATCH  /api/v1/{table}/{key}  # Partial update record
DELETE /api/v1/{table}/{key}  # Delete record
```

`{key}` is **one path segment per primary-key column, named after the column**,
and the arity is fixed at startup from the schema — a call with the wrong
number of segments matches no route at all:

```bash
GET    /api/v1/users/42             # PRIMARY KEY (id)          -> /{id}
GET    /api/v1/orders/1001          # PRIMARY KEY (order_id)    -> /{order_id}
DELETE /api/v1/order_lines/5/2      # PRIMARY KEY (order_id, line_no)
```

A table **with no primary key gets no key-addressed routes**: only the list and
create endpoints are generated for it. It used to receive routes built on a
fabricated `id` column, which reached the database and failed there.

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

Warp enriches the OpenAPI spec (`/openapi.json`) with catalog metadata, which makes the API self-documenting for both humans and LLMs. The context is refreshed after analyze/approve/delete, and several databases are merged into one `x-llm-context` (`{"databases": [...]}`).

The extension has two halves and they arrive at different times:

- **Structure is published from the first startup**, with no catalog and no LLM pass at all. Column types, the whole composite primary key, foreign keys with their targets and nullability come from the engine's own catalog — exactly, and for nothing. A consumer can see that `musteri_id` points at another table without waiting for a model.
- **Descriptions, human names and semantic types** need a model and a reviewer, so they appear only once a catalog is **approved**. A draft publishes none of them.

The structural stand-in is deliberately **not** marked approved and carries **no** semantic types, so nothing that gates on review can be satisfied by it — masking in particular reads only an approved catalog and keys on semantic types.

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
│       ├── db/                 # PostgreSQL/MySQL/ODBC (SQL Server, Oracle) gateways, Dialect table,
│       │                       #   SafeQueryBuilder, identifiers, named params, comment/sample readers
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

# Install with dev, LLM and ODBC dependencies
pip install -e ".[dev,llm,odbc]"

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
| `MSSQL_HOST` | `localhost` | SQL Server host (optional `mssql` entry / compose profile) |
| `MSSQL_PORT` | `1433` | SQL Server port |
| `MSSQL_DB` | `master` | SQL Server database name |
| `MSSQL_USER` | `sa` | SQL Server user |
| `MSSQL_PASS` | - | SQL Server password (required by the `mssql` compose profile) |
| `ORACLE_HOST` | `localhost` | Oracle host (optional `oracle` entry / compose profile) |
| `ORACLE_PORT` | `1521` | Oracle listener port |
| `ORACLE_SERVICE` | `FREEPDB1` | Oracle service name (not a schema) |
| `ORACLE_USER` | `warp` | Oracle user; also the schema introspected by default |
| `ORACLE_PASS` | - | Oracle password (required by the `oracle` compose profile) |
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
