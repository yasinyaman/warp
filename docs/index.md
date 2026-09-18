# Warp Engine

Auto-discovery REST CRUD API generator with LLM-powered database catalog
intelligence.

Warp connects to your database, discovers tables and columns, and generates a
fully functional REST API with filtering, sorting, pagination, and
authentication. Its catalog intelligence layer uses LLMs to generate
human-readable descriptions, semantic types, and multi-language documentation
for your schema — and injects all of it into the OpenAPI spec so downstream LLMs
and tools get rich context automatically.

## Highlights

- **Auto-discovery** of tables → full CRUD endpoints (PostgreSQL, MySQL & SQL Server;
  other ODBC sources best effort)
- **Filtering / sorting / pagination** with a safe, parameterized query layer
- **Typed schema and streaming export** (JSON, NDJSON, Arrow IPC) for external analytics engines
- **API-key auth** with role-based permissions
- **Catalog intelligence** — LLM-generated descriptions, semantic types, tags,
  relationships, multi-language docs, and a human-in-the-loop review workflow
- **OpenAPI enrichment** with `x-llm-context` extensions
- **Safe by default** — see [Production Deployment](deployment.md)

## Get started

See the [README](https://github.com/yasinyaman/warp#readme) for installation and
configuration. For a hardened production setup, read
[Production Deployment](deployment.md). For the rationale behind key design
choices, see the [Architecture Decisions](adr/README.md).
