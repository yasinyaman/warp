# 6. Distribution name vs. the import package

- Status: Accepted (applied in 0.9.0)
- Date: 2026-06-23

## Context

The project is named **Warp**, which collides with several well-known projects:

- **NVIDIA Warp** (`warp-lang` on PyPI) — a Python framework for GPU simulation.
- **Warp** (warpdotdev) — a popular terminal.
- Other smaller uses of "warp" across ecosystems.

The PyPI distribution name `warp` is effectively unavailable / ambiguous, and the
name is hard to discover and search for.

## Decision

If/when publishing to PyPI, use a namespaced **distribution** name while keeping
the **import** package as `warp`:

- distribution name: e.g. `warp-engine` (or `warp-crud`, `warp-api`)
- import name: `warp` (unchanged) — `pip install warp-engine` then `import warp`

This is a metadata-only change (`[project] name` in `pyproject.toml`) and does
not affect source code. Re-evaluate the import name only if it proves confusing.

## Consequences

- Avoids a name clash on PyPI and improves discoverability.
- Project marketing/name ("Warp Engine") stays intact.
- Deferred until publication; tracked here so it is not forgotten.
