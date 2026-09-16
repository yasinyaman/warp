# Architecture

Warp is a hexagonal (ports & adapters) application. Dependencies point inward
and are enforced by [import-linter](https://import-linter.readthedocs.io/)
contracts in `pyproject.toml`; run them locally with `lint-imports`.

## Layers (outside → inside)

| Layer | Package(s) | Responsibility |
|-------|-----------|----------------|
| Entry points | `warp.main`, `warp.cli` | The ASGI target / uvicorn runner and the `warp-catalog` console script; the only modules that call the composition root |
| Composition root | `warp.infrastructure` | `bootstrap.build_container()` wires outbound adapters into a `Container`; `config_loader` reads YAML + `${ENV}`; `logging` setup |
| Inbound adapters | `warp.adapters.inbound.http`, `warp.adapters.inbound.cli` | FastAPI app/lifespan, routes, auth dependencies, error mapping; Click commands |
| Outbound adapters | `warp.adapters.outbound.db`, `.llm`, `.catalog_store`, `.export` | PostgreSQL/MySQL gateways + SQL helpers + metadata readers, LLM providers, file-based catalog repository, exporters |
| Application | `warp.application` | Use-case services, ports (Protocols), configuration models, `Container` |
| Domain | `warp.domain` | Schema/catalog entities, review transitions, naming rules, sample/PII rules, filtering/sorting/pagination value objects, errors |

Contracts: a `layers` contract over the five layers, `independence` between
inbound and outbound adapters (and between http and cli), and `forbidden`
contracts keeping frameworks and drivers out of `domain` and `application`.

## Ports and their adapters

| Port (`warp.application.ports`) | Used by | Implemented by |
|---|---|---|
| `DatabaseGateway`, `SqlReader`, `DatabaseGatewayFactory` | CRUD, schema discovery, analysis, raw query, HTTP lifespan | `adapters.outbound.db.{postgres,mysql,factory}` |
| `CommentSource`, `SampleSource` | `CatalogAnalysisService` | `adapters.outbound.db.{comment_reader,sample_reader}` |
| `CatalogRepository` | review / cross-reference / analysis services, routes, CLI | `adapters.outbound.catalog_store.file_store` |
| `TextGenerator` | `CatalogAnalysisService` | `adapters.outbound.llm.client.LLMClient` (OpenAI, Anthropic, Gemini, Ollama providers) |
| `CatalogExporter` | `CatalogExportService` | `adapters.outbound.export.{json,yaml,markdown}` |

## Composition root

```
warp.main / warp.cli
  → infrastructure.config_loader.load_config()      Settings
  → infrastructure.bootstrap.build_container()      Container
      repository, review, export, gateway_factory,
      text_generator_factory, analysis_factory
  → adapters.inbound.http.create_app(container_factory, env)
    adapters.inbound.cli.main (factory injected via context_settings)
```

`Container.open_gateway()` / `open_analysis()` / `pipeline()` own the
lifetimes of what they open; tests pass fakes to `build_container()` instead of
patching modules.

## Request flow (CRUD)

```
HTTP request
  → adapters.inbound.http.routes.crud   (auth dependency; parse id/fields/filters by column kind)
    → application.services.crud        (mass-assignment whitelist)
      → DatabaseGateway port
        → adapters.outbound.db.postgres | mysql   (SafeQueryBuilder, identifier whitelist, bound params)
```

## Catalog intelligence flow

```
adapters.inbound.http.routes.catalog / adapters.inbound.cli
  → Container.open_analysis(db)
    → application.services.catalog_analysis.CatalogAnalysisService
        CommentSource + SampleSource (PII policy: domain.samples)
        TextGenerator (application.prompts)
        CrossReferenceService (other catalogs)
        CatalogReviewService → domain.catalog_review → CatalogRepository
  → application.services.openapi_enrichment (x-llm-context into the OpenAPI spec, approved catalogs only)
```

See the [Architecture Decisions](adr/README.md), in particular
[ADR-0007](adr/0007-hexagonal-architecture.md), for the rationale.
