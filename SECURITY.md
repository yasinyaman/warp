# Security Policy

## Reporting a vulnerability

**Please do not report security vulnerabilities through public GitHub issues.**

Instead, report them privately:

- Preferred: open a [GitHub private security advisory](https://github.com/yasinyaman/warp/security/advisories/new).
- Or email **yasnyaman@gmail.com** with details and reproduction steps.

Please include:

- a description of the issue and its impact,
- steps to reproduce (a minimal PoC if possible),
- affected version/commit, and
- any suggested remediation.

We aim to acknowledge reports within a few business days and will keep you
updated on remediation progress. Please give us reasonable time to release a fix
before any public disclosure.

## Supported versions

This project is pre-1.0 (current release: 0.9.x); security fixes target the
latest `main`. Pin to the hashed lock files (`requirements.lock`,
`requirements-prod.lock`) for reproducible, verified installs.

## Security posture

Warp is built with safe-by-default behavior. Key controls:

- **Fail-safe production config** — with `APP_ENV=production`, startup is refused
  when authentication is disabled, the raw SQL endpoint is enabled, or
  `CORS_ORIGINS` is `*`.
- **Parameterized SQL** — all values are bound parameters (raw SQL uses `:name`
  placeholders bound by `warp.adapters.outbound.db.params`); dynamic identifiers
  go through a single strict whitelist/quoting layer
  (`warp.adapters.outbound.db.identifiers`). Filter values are converted to the
  column's type rather than guessed from their shape.
- **Catalog endpoints** — every `/api/v1/catalog/*` route requires an API key
  with the matching permission (read / create for analysis / update / delete).
  Catalog names are validated and the file store never resolves a path outside
  its root.
- **OpenAPI spec** — `/openapi.json` carries catalog descriptions and
  `x-llm-context`; it is served through the auth manager and, in production,
  startup is refused while it is public unless `auth.allow_public_openapi: true`.
  Example values from real rows are only injected when
  `catalog.openapi_include_examples: true`.
- **Raw SQL endpoint** — disabled by default; when enabled it whitelists
  commands, rejects multiple statements, and can be pointed at a dedicated
  read-only database role (`readonly_username`/`readonly_password`) so a
  whitelist bypass still cannot write.
- **Authentication** — API keys are compared in constant time
  (`secrets.compare_digest`) against stored SHA-256 hashes; plaintext keys are
  not retained.
- **Mass-assignment protection** — create/update reject primary-key,
  auto-generated, and configured read-only columns.
- **Secrets** — provided via environment (`.env`, gitignored); no credentials in
  the repo. The container image runs as a non-root user.
- **LLM privacy** — sample data is not sent to cloud LLM providers unless opted
  in, PII-looking columns are masked before any LLM call, and sample values of
  PII columns (by name pattern or LLM semantic type) are never stored in the
  catalog.

See the README "Production Deployment" section and [`docs/adr/`](docs/adr/) for
details.
