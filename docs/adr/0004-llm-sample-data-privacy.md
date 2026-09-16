# 4. LLM sample-data privacy (cloud opt-in + PII masking)

- Status: Accepted
- Date: 2026-06-23

## Context

Catalog intelligence sends table samples to an LLM to generate descriptions.
Sample rows can contain personal or sensitive data. Sending that to a
third-party (cloud) LLM provider is a privacy risk that should never happen
implicitly.

## Decision

In `warp.enrichment.analyzer`, gate sample data before it reaches the prompt:

- **Cloud opt-in**: cloud providers (`openai`, `anthropic`, `gemini`) receive no
  raw samples unless `analysis.share_samples_with_cloud_llm` is enabled. Local
  providers (Ollama) are exempt.
- **PII masking**: when `analysis.mask_pii_samples` is enabled (default),
  values of columns whose names match PII patterns (email, phone, ssn, password,
  token, …) are masked to `***` before any LLM call.

The unmodified samples are still used to populate the catalog locally.

## Consequences

- No sensitive sample data leaves the machine to a cloud LLM by default.
- Pattern-based PII detection is heuristic (name-based); operators can extend
  `analysis.pii_column_patterns`. Semantic-type-based masking is a possible
  future enhancement once a catalog already exists.

## Addendum (0.9.0): storage-time masking and OpenAPI examples

Masking the LLM prompt was not enough: the stored catalog was re-published
through the draft API and injected into `/openapi.json` as `example` values.
Sample values of PII columns — matched by `analysis.pii_column_patterns` on the
name *or* by the LLM's `semantic_type` (`email`, `phone`, `name`, `address`) —
are therefore dropped before a `ColumnCatalogEntry` is built
(`warp.domain.samples.samples_for_storage`). Writing examples into the OpenAPI
spec is a separate opt-in, `catalog.openapi_include_examples` (default false),
and the spec route itself is protected by the auth manager (see the security
policy).
