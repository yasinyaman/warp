# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Row-level security.** An API key may carry `tenant`, `roles` and
  `row_filters`: mandatory per-table conditions, in the same vocabulary as user
  filters, with `${tenant}` / `${username}` resolved from the calling key.
  List, export and stream push them into the `WHERE` clause; reads and writes
  by primary key match the row in memory and report anything outside the policy
  as **404 rather than 403**, because refusing tells the caller the record
  exists. Writes cannot escape the scope — creating into another tenant, or
  moving a row there, is refused, as is omitting the scope column on create.
  A key with row rules is denied `query` (raw SQL) even when it holds `all`,
  since conditions cannot be pushed into a statement the caller wrote.
  `validate_production_config` refuses startup when row filters are configured
  while authentication is off, because then nobody is identified and the rules
  restrict nothing.
- **Column masking, keyed by the catalog's semantic types** rather than by
  column name, so a newly labelled PII column is masked as soon as it is
  discovered. Strategies: `partial`, `last4`, `hash` (a keyed pseudonym — see
  below), `redact`, `null`; a `NULL` stays `NULL`. Per-role overrides and
  exempt roles are supported. Applied to both row-carrying exits — CRUD
  responses and `/export` in all three formats. Only an *approved* catalog is
  used, and a mask that could not produce a value the response can carry (text
  on a numeric column, `null` on `NOT NULL`) is reported at startup.
- **`hash` is a keyed pseudonym, and it refuses to run without a key.** The
  strategy exists so the same value masks the same way in every row and every
  export, which is what keeps a masked column joinable — and that stability is
  exactly what makes an unkeyed digest reversible for an enumerable domain. A
  national id or an email has a small enough space to walk offline. It is now
  `BLAKE2b` keyed with `masking.hash_secret` (`WARP_MASKING_HASH_SECRET`),
  truncated to 128 bits. A `hash` rule with no secret fails where the
  configuration is *read*, in `rules` and in `by_role` alike, so the strategy
  can never quietly fall back to an unkeyed digest or to no masking at all; a
  secret shorter than 32 characters is a production violation. The key is an
  operator's to hold and to keep away from the exports — rotating it breaks
  correlation with older ones, which is the price of the stability.
- **Schema structure is published without waiting for the catalog.**
  `x-llm-context` used to be built entirely from the catalog, so a database
  with no approved one published nothing — including the half that was never
  the model's to say. Column types, the whole primary key, foreign keys with
  their targets and nullability come from the engine's own catalog, exactly
  and for nothing; only descriptions, human names and semantic types need a
  model and a reviewer. Enrichment now falls back to a structural catalog
  built from the schema already cached at startup, so a consumer can see that
  `urun_id` points at `urunler` on day one. The stand-in is deliberately left
  unapproved and carries no semantic types, so nothing that gates on review —
  masking above all — can be satisfied by it.
- **An audit trail.** One append-only event per data-touching request —
  actor, tenant, roles, action, database, table, row count, status, request id
  — recording **whether a row filter applied and which columns were masked**,
  which is the difference between "read the table" and "read their slice of
  it". Row values, filter literals and SQL parameters are never written: an
  audit log that quotes the data it audits becomes a second copy of it. Events
  go to a dedicated `warp.audit` logger and optionally to `audit.file`.
  Sinks never raise, because one that can fail a request turns a logging
  problem into an outage.
- Every response carries `X-Request-ID` (a caller-supplied one is honoured
  when it is safe to echo), and the same id appears on the audit event.
- `AuthenticatedUser` carries the caller's tenant, roles and row policy, and
  the dependency records it on `request.state` so handlers can reach it —
  FastAPI discards what a `dependencies=[...]` entry returns, so the identity
  built during authentication never reached a route before.

### Fixed

- **A row is addressed by its whole primary key, not by its first column.**
  `TableSchema.pk_column` answered the *first* column of a composite key and
  every key-addressed statement was built from that one column alone, with no
  row limit — so `DELETE /siparis_satirlari/5` emitted
  `DELETE FROM "siparis_satirlari" WHERE "siparis_id" = $1` and removed **every
  line of order 5**, answering `204` as if it had removed one. `UPDATE` did the
  same. On an ERP schema that is most of the line-item tables. Discovery was
  already correct — `primary_key` has always carried the full key — so the key
  was only ever lost on the way out; it now travels through the
  `DatabaseGateway` port, the three adapters and the query builder, which emits
  `a = $1 AND b = $2`. An empty key raises rather than producing a statement
  with no `WHERE` at all.

  Two changes are visible to clients. Key-addressed routes carry **one path
  segment per key column, named after the column** — `/{siparis_id}/{satir_no}`
  — so a single-key table keeps the same URL shape but its path *parameter* is
  now the real column name instead of `id`, which is also what a spec reader
  and a generated MCP tool see. And a table with **no primary key registers no
  key-addressed routes at all**; it used to get routes built on a fabricated
  `id` column, which reached the database and failed there.

- `MockDatabaseAdapter.select` in the test suite ignored its `filters`
  argument, so every route test that exercised `filter[column][op]` only
  proved the request did not error. It now filters as SQL would, which is what
  lets a test notice a *missing* condition.

### Added

- **Oracle support** through the existing ODBC adapter (`type: oracle`).
  Introspection from `ALL_TAB_COLS` / `ALL_CONSTRAINTS` / `ALL_CONS_COLUMNS` /
  `ALL_INDEXES` (with a pre-12c fallback to `ALL_TAB_COLUMNS` when
  `IDENTITY_COLUMN` raises ORA-00904), `FETCH FIRST` sampling,
  `OFFSET … FETCH` pagination, catalog intelligence from `ALL_TAB_COMMENTS` /
  `ALL_COL_COMMENTS` and row estimates from `ALL_TABLES.NUM_ROWS`. Oracle's
  schema is the connecting user, so it is resolved with `SELECT USER FROM DUAL`
  unless `options.schema` names one. Oracle integration tests run in CI
  (`WARP_REQUIRE_ORACLE=1`); unlike SQL Server the image has arm64 builds, so
  they also run natively on Apple Silicon. `docker compose --profile oracle`
  starts one locally.
- Oracle type mapping: `NUMBER`, `VARCHAR2`, `NVARCHAR2`, `BINARY_FLOAT`,
  `BINARY_DOUBLE`, `RAW`, `LONG RAW`, `BFILE`, `ROWID` and
  `TIMESTAMP WITH LOCAL TIME ZONE`. Oracle reports declared precision inside
  the type name (`TIMESTAMP(6) WITH TIME ZONE`), so the classifier now retries
  the lookup without it. A `NUMBER` with no declared precision has no
  `decimal128` that can hold it and is exported as text.
- Oracle columns declared as bare `NUMBER` (no precision) are detected during
  introspection, marked `inexact` in the typed schema and warned about once
  per table. The ODBC driver returns them as IEEE doubles, so a value above
  2^53 is rounded before Warp sees it and cannot be recovered afterwards;
  declaring a precision (`NUMBER(38)`) makes the driver hand over an exact
  `Decimal`. Verified both ways against a real Oracle 23.
- `Dialect` gained `sample_style`, `unsorted_pagination_filler`,
  `identifier_case` and `identifier_extra_chars`, plus a `fold_identifier`
  helper (see ADR-0008). `sanitize_identifier` now takes the engine's extra
  characters: Oracle allows `$` and `#` and its data dictionary generates them
  (an identity column's sequence is `ISEQ$$_73346`). The double quote stays
  rejected for every engine, so a quoted identifier is still unescapable.

Five of these were found by running the suite against a real Oracle rather
than by reading the docs:

- The ODBC connection string was assembled SQL-Server-style for every engine.
  Oracle's driver takes an Easy Connect descriptor in `DBQ`
  (`host:port/service`) and has no `SERVER`/`DATABASE` pair, so it answered
  ORA-12162.
- Oracle folds unquoted *column aliases* to upper case too, so every
  `AS column_name` in the Oracle catalog queries came back as `COLUMN_NAME`
  and introspection raised `KeyError`. The aliases are now quoted.
- `CHAR_LENGTH`/`DATA_PRECISION`/`DATA_SCALE` are `NUMBER`, which pyodbc hands
  back as `float` — producing `varchar2(50.0)` and a precision `pa.decimal128`
  refuses. They are narrowed to `int`.
- `insert` returned the *input* dict on the re-select path when the key was
  generated, because it looked the key up in the caller's data. Oracle now
  reads the identity column's sequence with `CURRVAL` on the **same** pooled
  connection (it is session state, so two connections would be wrong).
- `SampleReader.read_column_stats` read `distinct_count`/`null_count` by name
  and so returned all-`None` statistics on any driver that upper-cases result
  names. It now reads by position, as the ODBC row-count path already did.

### Fixed

- `sample_select` derived its syntax from `limit_style`, so any engine using
  `OFFSET … FETCH` pagination got SQL Server's `SELECT TOP (n)`.
- `order_and_limit` injected SQL Server's `ORDER BY (SELECT NULL)` filler for
  every `offset_fetch` engine.
- The PostgreSQL adapter hardcoded `table_schema = 'public'` in its table,
  column, primary-key, foreign-key and row-estimate queries, ignoring
  `options.schema` even though `Dialect.default_schema` and the metadata
  readers honour it. Its index query was not schema-scoped at all, so a
  same-named table in another schema contributed its indexes.

## [0.10.0] - 2026-09-18

SQL Server support, and a data-access layer for external analytics engines:
a typed schema endpoint, a streaming export (JSON, NDJSON, Arrow IPC) and a
`/info` capabilities block so clients can detect what a running Warp supports.

### Added

- `GET /schema` and `GET /{table}/schema`: typed columns (`kind`, precision,
  scale, max length, nullability, default), the primary key as a list and a
  planner **row estimate** (`pg_class.reltuples` / `information_schema.TABLES`,
  never a `COUNT(*)`). New port method `DatabaseGateway.row_estimates`.
- `GET|POST /{table}/export`: rows are read through a server-side cursor
  (asyncpg cursor in a transaction, aiomysql `SSDictCursor`) and written to the
  response batch by batch, so exporting a large table keeps API memory flat.
  Formats: `json` (`{"items": [...], "row_count": N}` written incrementally),
  `ndjson`, and `arrow` (Arrow IPC stream, `application/vnd.apache.arrow.stream`)
  when the optional `warp-engine[arrow]` extra is installed (501 otherwise).
  `GET` takes the list endpoint's `fields`/`filter[col][op]`/`sort`/`limit`;
  `POST` takes a JSON body for long `in` lists. New port method
  `DatabaseGateway.stream_select`; `SafeQueryBuilder.build_stream_select`;
  `FilterParser.parse_conditions` for structured filters.
- `settings.export` (`enabled`, `max_rows`, `batch_size`,
  `statement_timeout_ms`). A cap applied by `max_rows` is announced with the
  `X-Export-Max-Rows` header; every export answers with `X-Export-Format` and
  `Cache-Control: no-store`. Disabled exports answer 403.
- `/info.capabilities`: `api_prefix`, `db_prefix`, `schema`, `export`
  (`enabled`, `formats`, `max_rows`, `batch_size`), `raw_query`, `filter_ops`.
- Arrow type mapping: int16/int32/int64, `decimal128(p, s)` for numerics with a
  known precision ≤ 38, float32/float64, bool (incl. MySQL `tinyint(1)`),
  `timestamp[us, UTC]` for `timestamptz`, naive `timestamp[us]`, date32,
  time64, binary; json/uuid/interval/arrays/enums and anything unknown are
  exported as text. The schema message is written before the first row.

- **SQL Server support** through a new ODBC adapter (`type: mssql` /
  `sqlserver`, `odbc` extra: aioodbc + pyodbc; Microsoft's `msodbcsql18`
  driver is a system package). Full schema discovery via `INFORMATION_SCHEMA`
  and `sys.*` (identity/computed columns, keys, indexes), `OUTPUT`-based writes
  with a per-table fallback for tables with triggers (error 334), `TOP` /
  `OFFSET … FETCH` pagination, a `datetimeoffset` converter, and catalog
  intelligence (comments from `MS_Description`, samples, row counts).
- A best-effort generic `type: odbc` profile for other ODBC data sources
  (`options.driver` or `options.connection_string`).
- `options.schema` selects the schema to introspect for any engine;
  `options.connection_string`, `options.driver`, `options.encrypt`,
  `options.trust_server_certificate`, `options.extra` and `options.pool_recycle`
  for ODBC connections.
- Docker image installs the SQL Server ODBC driver by default
  (`--build-arg WITH_MSSQL_ODBC=0` opts out); `docker compose --profile mssql`
  starts a local SQL Server; SQL Server integration tests run in CI
  (testcontainers) and auto-skip locally when the driver or an x86-64 engine is
  missing (`WARP_MSSQL_HOST` targets an existing server instead).

### Changed

- Every route is now mounted under `/api/v1/{db_name}/…` **always**; with a
  single database the bare `/api/v1/…` paths remain as an alias (hidden from
  the OpenAPI document). Clients no longer need to know how many databases a
  Warp serves to build a URL.
- Route order: `/{table}/schema` and `/{table}/export` are registered before
  the CRUD routes so they are not captured by `GET /{table}/{id}`. A table
  literally named `schema` or `export` is shadowed by these endpoints.

- Engine differences (placeholders, quoting, `ILIKE`/`LIKE`,
  `RETURNING`/`OUTPUT`/re-select, pagination, default schema, catalog queries)
  now live in one frozen `Dialect` per engine; the query builder, named
  parameter binder, identifier quoting, comment/sample readers and the
  composition root consume it instead of branching on type strings
  (ADR-0008). PostgreSQL/MySQL SQL is unchanged.
- `ColumnSchema.is_auto_generated` is the single definition of "the database
  fills this column" (auto-increment, serial, identity, computed) used by
  create models and required/insertable column checks. `requirements-prod.lock`
  now includes the `odbc` extra.

### Fixed

- MySQL comment discovery was scoped by the config entry *name* instead of the
  database name, so table/column comments were only found when the two matched.
- PostgreSQL `GENERATED … AS IDENTITY` columns are now reported as identity and
  no longer demanded (or accepted) on create.

## [0.9.0] - 2026-09-17

First versioned release: security hardening, a hexagonal (ports & adapters)
architecture, and CI quality gates. The distribution is published as
`warp-engine` (import name stays `warp`, see ADR-0006); 0.9.x signals that the
public API may still change before 1.0.

### Security

- Catalog names are validated (`^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$`) and the file
  store refuses any path outside its root: `DELETE /api/v1/catalog/%2e%2e` could
  previously remove the *parent* of the catalog directory.
- Every `/api/v1/catalog/*` endpoint is now behind the API-key auth manager
  (read / create / update / delete permissions); they were public even with
  `auth.enabled: true`. `/analyze` no longer echoes exception text.
- PII never reaches the stored catalog: sample values of columns that look
  like PII by name *or* by LLM semantic type are dropped at storage time (the
  old masking only covered the LLM prompt). Example values are written into
  the OpenAPI spec only when `catalog.openapi_include_examples: true`.
- `/openapi.json` is an explicit route guarded by the auth manager; in
  production, startup is refused while it is listed in `auth.public_paths`
  unless `auth.allow_public_openapi: true`. Public-path matching is now on
  path-segment boundaries (`/health` no longer covers `/healthz`).
- Fail-safe production config validation, centralized SQL identifier
  quoting, timing-safe hashed API keys, mass-assignment protection, a
  default-off raw SQL endpoint with an optional read-only DB role, cloud-LLM
  sample-data opt-in, `.env`-based secrets and a hardened multi-stage
  Dockerfile (from the earlier hardening passes).

### Added

- Hexagonal architecture: `domain` / `application` (ports + services) /
  `adapters` (inbound http & cli, outbound db, llm, catalog store, export) /
  `infrastructure` (composition root, config loader, logging), enforced by
  five import-linter contracts (ADR-0007).
- `Container` + `build_container()` composition root; `RuntimeEnv` for
  process-level settings; `AnalysisReport` with `failed_tables` /
  `fallback_tables` returned by `POST /catalog/analyze` and printed by the CLI.
- Named-parameter binding for raw SQL (`:name`, `::cast`-safe, repeated names,
  missing/unused names rejected) shared by both database adapters.
- GitHub Actions CI installing from the hashed lock (`ruff`, `ruff format`,
  `mypy --strict`, import-linter, pytest + 80% coverage gate; pip-audit
  advisory), Dependabot, a runtime-only `requirements-prod.lock` used by the
  Docker image, `make lock`.
- Community files (CONTRIBUTING, SECURITY, CODE_OF_CONDUCT), mkdocs site,
  architecture decision records 0001-0007.

### Changed

- **Breaking:** module layout and import paths (e.g. `warp.database.postgres`
  → `warp.adapters.outbound.db.postgres`, `warp.catalog.store` →
  `warp.adapters.outbound.catalog_store.file_store`); `AutoCrudException` →
  `WarpError`; `EnrichedAnalyzer` → `CatalogAnalysisService` with injected
  collaborators; `CatalogFileStore` is persistence-only (review transitions
  live in `warp.domain.catalog_review` / `CatalogReviewService`).
- **Breaking:** distribution name `warp` → `warp-engine`; version 1.0.0 →
  0.9.0.
- Filter values are converted according to the column's type instead of the
  shape of the text (`filter[zip]=00123` stays text); bad ids answer 422,
  unknown `fields`/filter columns and bad sort answer 400 (were 500s).
- `pagination.max_limit` is honoured end to end (the value object no longer
  caps at 1000).
- The catalog store owns its file format (`default_format`); re-saves never
  leave a stale sibling file, so a `yaml` re-analysis is no longer ignored.
- OpenAPI enrichment publishes approved catalogs only, refreshes after
  analyze/approve/delete, and merges several databases into one
  `x-llm-context`.
- Formatting moved from black + isort to `ruff format`; Gemini provider moved
  to the `google-genai` SDK; typing modernized to PEP 585/604.

### Fixed

- MySQL: `UPDATE` with unchanged values returned 500 (rowcount counted changed
  rows; the pool now requests `FOUND_ROWS`); `get_tables()` raised `KeyError`
  on MariaDB/MySQL 5.7.
- LLM error classification matched the substring "rate" (so "generateContent"
  errors were reported as quota problems).
- CLI/pipeline flattened `options` so pool size/SSL never reached the adapter.
- `warp.main` was unimportable after the typing pass (`response_model=None`).
- CI: install step failed under PEP 668; pinned actions targeted deprecated
  Node 20.

### Removed

- Dead code: `query/strategy.py`, `integration/mcp_enricher.py`, the settings
  and auth-manager singletons, `list_drafts`, `build_cross_reference_context`,
  `ExportError`/`I18nError`, the `get_logger` wrapper, the empty
  `tests/integration` package and unused pytest markers; black and isort.

[Unreleased]: https://github.com/yasinyaman/warp/compare/v0.9.0...HEAD
[0.9.0]: https://github.com/yasinyaman/warp/releases/tag/v0.9.0
