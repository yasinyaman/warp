# 7. Hexagonal architecture (ports & adapters)

- Status: Accepted
- Date: 2026-09-17
- Supersedes: [ADR-0001](0001-layered-architecture.md)

## Context

The layered contracts of ADR-0001 kept packages from importing "upwards", but
they could not express the properties that actually matter for this code base:
the domain must not know FastAPI or asyncpg; the analysis use case must not
construct database readers, an LLM client and a file store itself; and the
wiring must exist in exactly one place. In practice composition was duplicated
across `main.py`, `cli.py` and `integration/pipeline.py` (with a bug in two of
them), the catalog file store mixed persistence with the review workflow, an
HTTP handler built the whole enrichment pipeline inline, and module-level
singletons (`AppState`, `_settings`, `_auth_manager`) made the app hard to test.

## Decision

Four layers with a strict inward dependency direction, enforced by
import-linter (`[tool.importlinter]` in `pyproject.toml`):

| Layer | Package | Rules |
|-------|---------|-------|
| Entry points | `warp.main`, `warp.cli` | may import anything; own the process (uvicorn, console script) |
| Composition root | `warp.infrastructure` | builds outbound adapters and the `Container`; loads config; sets up logging |
| Adapters | `warp.adapters.inbound.{http,cli}`, `warp.adapters.outbound.{db,llm,catalog_store,export}` | may import `application` and `domain` only; inbound and outbound are independent of each other, as are http and cli |
| Application | `warp.application` | use-case services, ports (`typing.Protocol`), configuration models, the `Container` type; imports `domain` only; no frameworks/drivers |
| Domain | `warp.domain` | entities and pure rules (stdlib + pydantic); imports nothing else |

Key mechanics:

- **Ports** (`warp.application.ports`): `DatabaseGateway`/`SqlReader`/
  `DatabaseGatewayFactory`, `CommentSource`/`SampleSource`,
  `CatalogRepository`, `TextGenerator`, `CatalogExporter`. Adapters satisfy
  them structurally; tests substitute fakes without patching.
- **Composition root**: `infrastructure.bootstrap.build_container(settings,
  *, repository=, gateway_factory=, text_generator_factory=, analysis_factory=)`
  returns an `application.container.Container`. The HTTP app takes a
  *container factory* (`create_app(container_factory, env)`), the CLI group
  receives one through `context_settings["obj"]`; neither adapter imports
  `infrastructure`.
- **Runtime state** lives on `app.state.runtime` (`RuntimeContext`) and in
  `RuntimeEnv`, not in module globals.
- **Review workflow** transitions are pure functions in
  `domain.catalog_review`; `CatalogReviewService` applies them over the
  repository; `CatalogFileStore` only persists.
- No compatibility shims: 0.9.0 changes import paths outright.

## Consequences

- Import paths changed (breaking); the test tree mirrors the layers.
- Any adapter can be swapped in tests or by embedders (in-memory repository,
  fake gateway, recorded LLM) by passing it to `build_container`.
- The full startup sequence is exercised in tests over a fake gateway.
- Adding a database or LLM provider means adding an outbound adapter that
  satisfies the port; no application code changes.
