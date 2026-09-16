# Production Deployment (secure defaults)

Warp fails safe: when `APP_ENV=production` the app **refuses to start** with
unsafe configuration.

## Checklist

1. **Secrets via environment, never in the repo.** Copy `.env.example` → `.env`
   (gitignored) and set strong values. Never commit real credentials or keys.
2. **`APP_ENV=production`** — disables `/docs` & `/redoc`, switches logs to JSON,
   and enables the startup safety checks below.
3. **Authentication on** — `settings.auth.enabled: true` with real API keys via
   `API_KEY_*` env vars. Startup is refused if auth is off in production.
4. **Explicit CORS allowlist** — `CORS_ORIGINS=https://app.example.com,...`.
   `*` is rejected in production; credentials are only sent with an explicit
   allowlist.
5. **Raw SQL endpoint off** — `enable_raw_query: false` (default). Startup is
   refused if it is enabled in production. If you must enable it, configure a
   **read-only** database role via `databases[].readonly_username` /
   `readonly_password` (env `DB_READONLY_USER` / `DB_READONLY_PASS`); raw queries
   then run as that role, so a whitelist bypass still cannot write.
6. **Run the production image** — the `Dockerfile` is multi-stage, runs as a
   non-root user, ships no dev dependencies, and has no `--reload`. Mount a
   hardened `config/database.yaml` and pass secrets via the environment.
7. **Reproducible installs** — `uv pip sync --require-hashes requirements.lock` for deterministic,
   hash-verified dependencies.
8. **Least-privilege DB account** — grant only what the API needs; restrict
   write access to the tables that should be writable.
9. **Catalog endpoints are authenticated** — `/api/v1/catalog/*` needs a key
   with `read` (views), `create` (analysis), `update` (edits/approvals) or
   `delete`. Keep `catalog.openapi_include_examples: false` unless the spec
   audience may see real row values.

## Startup safety checks

`validate_production_config()` (in `warp.application.config`) runs during app
startup. With `APP_ENV=production` it raises `ConfigurationError` (refusing to
start) when any of these hold:

- `auth.enabled` is `false`
- `settings.enable_raw_query` is `true`
- `CORS_ORIGINS` contains `*`
- `/openapi.json` is in `auth.public_paths` (the enriched spec exposes catalog
  descriptions and `x-llm-context`) unless `auth.allow_public_openapi: true`

This guarantees a misconfigured production deployment fails loudly at boot rather
than silently exposing the API.

## Always-on protections

Parameterized queries, mass-assignment protection, timing-safe API-key checks,
and PII masking before any cloud LLM call are enabled by default regardless of
environment. See the [Security Policy](https://github.com/yasinyaman/warp/blob/main/SECURITY.md).
