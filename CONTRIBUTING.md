# Contributing to Warp Engine

Thanks for your interest in contributing! This guide covers local setup and the
checks your change must pass.

## Development setup

```bash
git clone https://github.com/yasinyaman/warp.git
cd warp

python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,llm]"        # or: uv pip sync requirements.lock
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
mypy src/                          # mypy --strict must be clean
lint-imports                       # layered-architecture contracts must hold
```

CI runs the same checks on Python 3.11 and 3.12 (`.github/workflows/ci.yml`).
`pip-audit` runs as well (advisory).

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
- **Commits:** small, focused commits with clear messages.

## Pull requests

1. Branch from `main`.
2. Make your change with tests and docs.
3. Ensure every gate above is green.
4. Open a PR describing the change and rationale.

## Reporting bugs / security issues

For functional bugs, open an issue. For security vulnerabilities, **do not** open
a public issue — follow [SECURITY.md](SECURITY.md).
