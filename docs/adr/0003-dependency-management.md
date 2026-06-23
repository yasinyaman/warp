# 3. Dependency management: ranges + hashed lockfile

- Status: Accepted
- Date: 2026-06-23

## Context

The project is consumed both as a library (`pip install`) and deployed as an
application (Docker). Libraries want flexible version ranges to avoid conflicts
for downstream consumers; deployments want exact, reproducible, verifiable
dependencies.

## Decision

- Keep flexible lower-bound ranges (`>=`) in `pyproject.toml` for library
  consumers.
- Generate a universal, hashed lockfile `requirements.lock` with
  `uv pip compile pyproject.toml --all-extras --universal --generate-hashes`.
- Use the lockfile for reproducible installs in CI, Docker, and audits
  (`uv pip sync requirements.lock` / `pip install --require-hashes`).
- Migrated the Gemini provider off the EOL `google-generativeai` SDK to
  `google-genai`.
- Dependabot keeps both Python deps and the SHA-pinned GitHub Actions current.

## Consequences

- Library consumers retain resolution flexibility; deployments are deterministic
  and tamper-evident (hashes).
- The lockfile must be regenerated when `pyproject.toml` dependencies change.
