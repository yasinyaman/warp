# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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
