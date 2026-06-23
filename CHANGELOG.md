# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Security hardening, supply-chain, and quality pass.

### Security

- Fail-safe production config validation: with `APP_ENV=production`, startup is
  refused when auth is disabled, the raw SQL endpoint is enabled, or
  `CORS_ORIGINS` is `*`.
- Centralized strict SQL identifier sanitization/quoting
  (`warp.database.identifiers`); replaced the weak `.replace()`-based quoting in
  the sample reader.
- API keys: timing-safe comparison (`secrets.compare_digest`) against stored
  SHA-256 hashes; plaintext keys no longer retained.
- Mass-assignment protection on create/update (rejects primary-key,
  auto-generated, and configured read-only columns).
- Raw SQL endpoint: disabled by default, multiple-statement rejection, generic
  error responses (no schema leakage), documented read-only-role expectation.
- LLM sample-data privacy: cloud providers receive no raw samples unless opted
  in (`analysis.share_samples_with_cloud_llm`); PII columns masked before any
  LLM call (`analysis.mask_pii_samples`).
- Secrets moved out of the repo into `.env` (with `.env.example`); removed weak
  default API keys and hardcoded database passwords.
- Hardened `Dockerfile`: multi-stage, non-root user, no dev dependencies, no
  `--reload`.

### Added

- GitHub Actions CI (ruff, mypy --strict, import-linter, pytest + coverage gate,
  pip-audit), Dependabot, SHA-pinned actions, least-privilege permissions.
- Hashed, universal lockfile (`requirements.lock`).
- Layered-architecture contracts enforced by import-linter.
- `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, architecture decision
  records (`docs/adr/`), and an mkdocs documentation site.
- Test suite expanded to 647 tests; coverage raised to ~84% (80% gate enforced).

### Changed

- Migrated the Gemini provider from the EOL `google-generativeai` SDK to
  `google-genai`.
- Modernized typing across the codebase to PEP 585/604; `ruff` and
  `mypy --strict` are now clean and enforced.
- Exceptions are chained with `raise ... from` (B904).

### Fixed

- `warp.main` was unimportable after the type-annotation pass: `/health` and
  `/ready` needed `response_model=None` for their `dict | JSONResponse` return
  type (FastAPI raised at app build time). Added a regression guard.
- Stale tests from the catalog-intelligence redesign.
- Project metadata: corrected repository URLs, license classifier
  (MIT → Apache), and author/contact fields.
