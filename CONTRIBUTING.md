# Contributing to Warp Engine

Thanks for your interest in contributing! This guide covers local setup and the
checks your change must pass.

## Development setup

```bash
git clone https://github.com/yasinyaman/warp.git
cd warp

# Reproducible (what CI runs): hash-verified lock via uv
uv venv .venv && source .venv/bin/activate
uv pip sync --require-hashes requirements.lock
uv pip install --no-deps -e .

# Or plain pip with the version ranges from pyproject.toml
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,llm]"
```

Copy `.env.example` to `.env` and fill in values for local Docker work:

```bash
cp .env.example .env
make docker-up                     # API + PostgreSQL + MySQL + Adminer
```

## Quality gates (all enforced in CI)

Run them locally before opening a PR:

```bash
pytest                             # tests must pass
pytest --cov=src/warp              # coverage must stay >= 80%
ruff check src/ tests/             # lint must be clean
ruff format --check src/ tests/    # formatting must match `ruff format`
mypy src/                          # mypy --strict must be clean
lint-imports                       # layered-architecture contracts must hold
```

CI runs the same checks on Python 3.11 and 3.12 (`.github/workflows/ci.yml`).
`pip-audit` runs as well (advisory). `make lint` runs all of them locally.

### Dependencies

Version ranges live in `pyproject.toml`; exact, hash-pinned versions live in
`requirements.lock` (all extras, used by CI) and `requirements-prod.lock`
(runtime + `llm`, used by the Docker image). After changing dependencies in
`pyproject.toml`, regenerate both with `make lock` (needs [uv](https://docs.astral.sh/uv/))
and commit them together.

### Conventions

- **Typing:** modern syntax only (`dict[str, Any]`, `list[...]`, `X | None`).
  `mypy --strict` must pass — annotate new code fully.
- **Architecture:** respect the import contracts in `pyproject.toml`
  (`[tool.importlinter]`). `core`/`config` are the lowest layers; `database`
  imports no app/domain modules; `catalog` must not depend on `llm`/`enrichment`/
  `api`. See the ADRs in [`docs/adr/`](docs/adr/).
- **Tests:** keep them hermetic — no real database, network, or LLM calls (mock
  them). New behavior needs tests; coverage must not drop below 80%.
- **SQL:** never interpolate values into SQL — use bound parameters. Dynamic
  identifiers must go through `warp.database.identifiers`.
- **Formatting:** `ruff format` (run `make format`); no other formatter.
- **Commits:** small, focused commits with clear messages.

## Pull requests

1. Branch from `main`.
2. Make your change with tests and docs.
3. Ensure every gate above is green.
4. Open a PR describing the change and rationale.

## Reporting bugs / security issues

For functional bugs, open an issue. For security vulnerabilities, **do not** open
a public issue — follow [SECURITY.md](SECURITY.md).
