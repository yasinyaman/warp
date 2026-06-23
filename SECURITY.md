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

This project is pre-1.0; security fixes target the latest `main`. Pin to the
hashed lockfile (`requirements.lock`) for reproducible, verified installs.

## Security posture

Warp is built with safe-by-default behavior. Key controls:

- **Fail-safe production config** — with `APP_ENV=production`, startup is refused
  when authentication is disabled, the raw SQL endpoint is enabled, or
  `CORS_ORIGINS` is `*`.
- **Parameterized SQL** — all values are bound parameters; dynamic identifiers go
  through a single strict whitelist/quoting layer (`warp.database.identifiers`).
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
  in, and PII-looking columns are masked before any LLM call.

See the README "Production Deployment" section and [`docs/adr/`](docs/adr/) for
details.
