"""Sample-data value objects and the PII policy helpers that act on them.

Pure: no database access, no configuration objects.
"""

import copy
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

# Column-name substrings that indicate likely PII. Single source of truth for
# both the config default and the enrichment masking helpers.
DEFAULT_PII_PATTERNS: tuple[str, ...] = (
    "email",
    "mail",
    "phone",
    "tel",
    "mobile",
    "ssn",
    "social_security",
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "credit_card",
    "card_number",
    "cardno",
    "cvv",
    "iban",
    "account_number",
    "tax_id",
    "passport",
    "national_id",
)

# LLM-assigned semantic types whose values are personal data regardless of the
# column name (e.g. a column called "contact" typed as "email").
PII_SEMANTIC_TYPES: frozenset[str] = frozenset({"email", "phone", "name", "address"})


_MASK_VALUE = "***"


@dataclass
class ColumnStats:
    """Statistics for a single column."""

    column_name: str
    distinct_count: int | None = None
    null_count: int | None = None
    sample_values: list[Any] = field(default_factory=list)


@dataclass
class TableSamples:
    """Sample data and stats for a table."""

    table_name: str
    row_count: int | None = None
    column_samples: dict[str, list[Any]] = field(default_factory=dict)
    column_stats: dict[str, ColumnStats] = field(default_factory=dict)


def is_pii_column(name: str, patterns: Sequence[str] | None = None) -> bool:
    """Heuristically decide whether a column name looks like PII."""
    lowered = name.lower()
    return any(p in lowered for p in (patterns or DEFAULT_PII_PATTERNS))


def is_pii_semantic_type(semantic_type: str | None) -> bool:
    """Whether an LLM-assigned semantic type denotes personal data."""
    return bool(semantic_type) and str(semantic_type).lower() in PII_SEMANTIC_TYPES


def mask_pii_samples(samples: TableSamples, patterns: Sequence[str] | None = None) -> TableSamples:
    """Return a copy of ``samples`` with PII-looking column values masked.

    Intended for use before sending sample data to an LLM: values of columns
    whose name matches a PII pattern are replaced with ``"***"`` while
    non-PII columns and all column statistics counts are left intact.
    """
    patterns = patterns or DEFAULT_PII_PATTERNS
    masked = copy.deepcopy(samples)

    for col_name, values in masked.column_samples.items():
        if is_pii_column(col_name, patterns):
            masked.column_samples[col_name] = [_MASK_VALUE for _ in values]

    for col_name, stats in masked.column_stats.items():
        if is_pii_column(col_name, patterns) and stats.sample_values:
            stats.sample_values = [_MASK_VALUE for _ in stats.sample_values]

    return masked


def samples_for_storage(
    column_name: str,
    semantic_type: str | None,
    values: list[Any],
    *,
    mask_pii: bool = True,
    patterns: Sequence[str] | None = None,
) -> list[Any]:
    """Decide which sample values may be persisted in the catalog for a column.

    Masking at LLM-prompt time is not enough: stored ``sample_values`` are
    re-published through the draft API and the OpenAPI spec. When ``mask_pii``
    is on, columns that look like PII by name pattern *or* by the LLM's
    semantic type keep no sample values at all.

    Returns:
        ``values`` unchanged, or an empty list for PII columns.
    """
    if not mask_pii:
        return values
    if is_pii_column(column_name, patterns) or is_pii_semantic_type(semantic_type):
        return []
    return values
