# Architecture

Warp is organized into layers with explicit dependency direction, enforced by
[import-linter](https://import-linter.readthedocs.io/) contracts in
`pyproject.toml`.

## Layers (low → high)

| Layer | Package(s) | Responsibility |
|-------|-----------|----------------|
| Foundation | `core`, `config` | Exceptions, logging, settings — depend on nothing else in `warp` |
| Data access | `database`, `utils`, `schema` | Adapters (PostgreSQL/MySQL), filtering/sorting/pagination, schema models |
| Domain | `catalog`, `llm`, `enrichment`, `i18n`, `query`, `export` | Catalog storage, LLM clients/prompts, enrichment pipeline |
| Application | `api`, `integration` | FastAPI routers, auth, CRUD, OpenAPI/MCP enrichment |
| Entry points | `main`, `cli` | App factory + lifespan, Click CLI |

## Enforced contracts

- `core` and `config` import nothing else in `warp`.
- `database` imports no app/domain modules.
- `catalog` must not depend on `llm`, `enrichment`, `api`, or `integration`.
- `schema` and `utils` must not depend on `api` or `integration`.

Run them locally with `lint-imports`.

## Request flow (CRUD)

```
HTTP request
  → api/router_factory (route + auth dependency)
    → utils/filtering, utils/sorting   (validate columns against the schema)
    → api/crud                         (mass-assignment whitelist)
      → database/postgres | mysql      (parameterized SQL, identifier whitelist)
        → connection pool
```

## Catalog intelligence flow

```
enrichment/analyzer
  → comment_reader, sample_reader (samples; PII masked / cloud opt-in)
  → llm/client + llm/prompts      (descriptions, semantic types)
  → catalog/store                 (draft → review → approve)
  → integration/openapi_enricher  (inject x-llm-context into the OpenAPI spec)
```

See the [Architecture Decisions](adr/README.md) for the rationale behind these
choices.
