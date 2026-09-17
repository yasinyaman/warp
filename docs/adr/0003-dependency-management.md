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
- Generate two universal, hashed lock files with `make lock`:
  `requirements.lock` (all extras; CI and local development) and
  `requirements-prod.lock` (runtime + `llm` + `odbc`; the Docker image).
- Install from the locks with `--require-hashes` (`uv pip sync`) in CI and in
  the Dockerfile builder stage; never resolve from ranges in a deployment.
- Migrated the Gemini provider off the EOL `google-generativeai` SDK to
  `google-genai`.
- Dependabot keeps both Python deps and the SHA-pinned GitHub Actions current.

## Consequences

- Library consumers retain resolution flexibility; deployments are deterministic
  and tamper-evident (hashes).
- Both lock files must be regenerated (`make lock`) whenever `pyproject.toml`
  dependencies change; CI installs from them, so a stale lock fails fast.
