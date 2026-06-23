# 5. CI quality gates: pragmatic, then strict

- Status: Accepted
- Date: 2026-06-23

## Context

When CI was introduced, the codebase did not yet pass its own intended gates:
~597 ruff errors, 147 `mypy --strict` errors, ~49% coverage, and 4 broken tests.
Making every gate blocking immediately would have left CI red and blocked all
work; making none blocking would let quality keep drifting.

## Decision

Introduce CI green from day one, then ratchet each gate to blocking as the debt
was cleared:

1. Start: `pytest` blocking; ruff / mypy / coverage non-blocking (reported).
2. Clear the backlog incrementally — fix the broken tests, clean ruff to zero,
   fix all `mypy --strict` errors, raise coverage to ≥80%.
3. Flip each gate to blocking once it passes.

End state — all blocking and green: `pytest`, coverage ≥80%, `ruff`,
`mypy --strict`, `lint-imports`. `pip-audit` runs advisory (non-blocking) to
avoid surprise breakage from new advisories. Actions are SHA-pinned with
least-privilege `permissions`.

## Consequences

- CI was useful immediately and never blocked progress on pre-existing debt.
- Quality cannot regress below the current bar.
- Adding new rule sets (e.g. ruff `D`/`PL`/`C901`) is deferred — they would add
  ~789 errors (mostly docstrings) and belong to a dedicated pass.
