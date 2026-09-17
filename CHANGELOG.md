# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.10.0] - 2026-09-18

Data-access release for external analytics engines: a typed schema endpoint,
a streaming export (JSON, NDJSON, Arrow IPC) and a `/info` capabilities block
so clients can detect what a running Warp supports.

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

### Changed

- Every route is now mounted under `/api/v1/{db_name}/…` **always**; with a
  single database the bare `/api/v1/…` paths remain as an alias (hidden from
  the OpenAPI document). Clients no longer need to know how many databases a
  Warp serves to build a URL.
- Route order: `/{table}/schema` and `/{table}/export` are registered before
  the CRUD routes so they are not captured by `GET /{table}/{id}`. A table
  literally named `schema` or `export` is shadowed by these endpoints.

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
