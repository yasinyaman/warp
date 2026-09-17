# 1. Layered architecture enforced by import-linter

- Status: Superseded by [ADR-0007](0007-hexagonal-architecture.md)
- Date: 2026-06-23

## Context

The codebase grew organically and had at least one circular dependency
(`llm` ↔ `enrichment`). Without an enforced dependency direction, low-level
modules risk importing high-level ones, making the system hard to reason about,
test, and reuse.

## Decision

Define explicit layers and enforce their dependency direction with
[import-linter](https://import-linter.readthedocs.io/) contracts in
`pyproject.toml` (`[tool.importlinter]`), run as a blocking CI gate
(`lint-imports`):

- `core` and `config` are the lowest layers and import nothing else in `warp`.
- `database` imports no app/domain modules.
- `catalog` must not depend on `llm`, `enrichment`, `api`, or `integration`.
- `schema` and `utils` must not depend on `api` or `integration`.

The `llm` ↔ `enrichment` runtime cycle was broken by importing `TableSamples`
under `TYPE_CHECKING` in `llm/prompts.py`.

## Consequences

- Dependency direction is verifiable and regressions are caught in CI.
- New code must respect the contracts (see CONTRIBUTING).
- A few contracts are intentionally conservative; they can be tightened (e.g. a
  strict total ordering) as the architecture stabilizes.
