"""Column masking, declared per semantic type rather than per column.

The catalog already labels every column with a *semantic type* — ``email``,
``phone``, ``amount`` and so on — reviewed by a human before it is approved.
Masking rules attach to those labels, not to column names, which is the point:
a newly discovered ``contact_email`` in another table is masked the day it is
discovered, instead of leaking until somebody remembers to write a rule for it.

A strategy has to produce a value the response still validates against, so the
string strategies only apply to text columns and ``null`` only to nullable
ones. A rule that cannot be applied safely is reported rather than guessed at;
see :func:`unsafe_rules`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

MaskStrategy = Literal["redact", "partial", "last4", "hash", "null"]

STRATEGIES: frozenset[str] = frozenset({"redact", "partial", "last4", "hash", "null"})

#: Strategies whose output is text, so they need a text column.
TEXT_STRATEGIES: frozenset[str] = frozenset({"redact", "partial", "last4", "hash"})

REDACTED = "***"


class MaskingError(ValueError):
    """A masking rule that cannot be applied as written."""


def mask_value(value: Any, strategy: str) -> Any:
    """Apply one strategy to one value.

    ``None`` stays ``None`` — masking a value that is not there would invent
    the appearance of data.
    """
    if value is None:
        return None
    if strategy == "null":
        return None
    text = str(value)
    if strategy == "redact":
        return REDACTED
    if strategy == "hash":
        # Stable, so the same value can still be correlated across rows, but
        # not reversible. Truncated: the full digest only adds length.
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    if strategy == "last4":
        return f"{REDACTED}{text[-4:]}" if len(text) > 4 else REDACTED
    if strategy == "partial":
        return _partial(text)
    raise MaskingError(f"Unknown masking strategy '{strategy}'")


def _partial(text: str) -> str:
    """Keep just enough to recognise a value without disclosing it.

    An address keeps its domain, because "which provider" is usually the part
    a support agent needs and the local part is the identifying half.
    """
    if "@" in text:
        local, _, domain = text.partition("@")
        head = local[0] if local else ""
        return f"{head}{REDACTED}@{domain}"
    if len(text) <= 2:
        return REDACTED
    return f"{text[0]}{REDACTED}{text[-1]}"


@dataclass(frozen=True)
class MaskingPolicy:
    """Which semantic types are masked, and how, for one caller.

    ``by_role`` overrides ``rules`` for a caller holding that role, and a role
    in ``exempt_roles`` sees raw values. Roles are checked in the order the
    caller lists them, so the first match wins.
    """

    rules: Mapping[str, str] = field(default_factory=dict)
    by_role: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    exempt_roles: tuple[str, ...] = ()
    enabled: bool = True

    @property
    def is_empty(self) -> bool:
        """Whether this policy would mask nothing at all."""
        return not self.enabled or not (self.rules or self.by_role)

    def for_roles(self, roles: Iterable[str]) -> dict[str, str]:
        """The semantic-type to strategy map that applies to these roles."""
        if not self.enabled:
            return {}
        listed = list(roles)
        if any(role in self.exempt_roles for role in listed):
            return {}
        for role in listed:
            override = self.by_role.get(role)
            if override is not None:
                return dict(override)
        return dict(self.rules)

    def columns_for(
        self, semantic_types: Mapping[str, str], roles: Iterable[str] = ()
    ) -> dict[str, str]:
        """Map a table's ``column -> semantic_type`` to ``column -> strategy``."""
        strategies = self.for_roles(roles)
        if not strategies:
            return {}
        return {
            column: strategies[semantic]
            for column, semantic in semantic_types.items()
            if semantic in strategies
        }


EMPTY_MASKING = MaskingPolicy(enabled=False)
"""Masks nothing — what an exempt caller or a disabled configuration gets."""


def mask_row(row: Mapping[str, Any], masks: Mapping[str, str]) -> dict[str, Any]:
    """A copy of ``row`` with the masked columns replaced."""
    if not masks:
        return dict(row)
    return {
        key: mask_value(value, masks[key]) if key in masks else value for key, value in row.items()
    }


def mask_rows(rows: Iterable[Mapping[str, Any]], masks: Mapping[str, str]) -> list[dict[str, Any]]:
    """``mask_row`` over a batch."""
    return [mask_row(row, masks) for row in rows]


def validate_strategy(strategy: str, semantic_type: str) -> str:
    """Check a configured strategy name.

    Raises:
        MaskingError: Naming the strategies that exist, so a typo does not
            quietly disable masking for that semantic type.
    """
    if strategy not in STRATEGIES:
        raise MaskingError(
            f"Unknown masking strategy '{strategy}' for semantic type "
            f"'{semantic_type}'. Available: {', '.join(sorted(STRATEGIES))}."
        )
    return strategy


def unsafe_rules(
    masks: Mapping[str, str], column_types: Mapping[str, tuple[str, bool]]
) -> list[str]:
    """Masks that would produce a value the response cannot carry.

    Args:
        masks: ``column -> strategy``.
        column_types: ``column -> (kind, nullable)`` from the discovered schema.

    Returns:
        Human-readable problems. Empty means every mask is applicable.
        Reporting these at startup is the point: a text mask on a numeric
        column, or a ``null`` mask on a ``NOT NULL`` one, would otherwise fail
        response validation on the first request that returned such a row.
    """
    problems: list[str] = []
    for column, strategy in masks.items():
        known = column_types.get(column)
        if known is None:
            continue
        kind, nullable = known
        if strategy in TEXT_STRATEGIES and kind != "str":
            problems.append(
                f"column '{column}' is {kind}, so the '{strategy}' mask would "
                f"replace it with text; use 'null' or leave it unmasked"
            )
        elif strategy == "null" and not nullable:
            problems.append(
                f"column '{column}' is NOT NULL, so the 'null' mask would "
                f"produce a value the response cannot carry"
            )
    return problems


@dataclass(frozen=True)
class CatalogMasking:
    """A masking policy plus the catalog labels it keys on.

    The two are useless apart — rules name semantic types, and only the
    catalog knows which column carries which — so they travel together.
    """

    policy: MaskingPolicy = field(default_factory=lambda: EMPTY_MASKING)
    semantic_types: Mapping[str, Mapping[str, str]] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        """Whether this would mask nothing, whatever the table or caller."""
        return self.policy.is_empty or not self.semantic_types

    def masks_for(self, table: str, roles: Iterable[str] = ()) -> dict[str, str]:
        """``column -> strategy`` for one table and one caller's roles."""
        labels = self.semantic_types.get(table)
        if not labels or self.policy.is_empty:
            return {}
        return self.policy.columns_for(labels, roles)


NO_MASKING = CatalogMasking()
"""Nothing configured, or no approved catalog to key rules on."""


def semantic_types_of(catalog: Any) -> dict[str, dict[str, str]]:
    """``table -> column -> semantic_type`` from an approved catalog.

    Only approved catalogs are used: a draft's labels have not been reviewed,
    and masking the wrong columns is as damaging as masking none.
    """
    if catalog is None or getattr(catalog, "status", None) != "approved":
        return {}
    by_table: dict[str, dict[str, str]] = {}
    for table_name, table in getattr(catalog, "tables", {}).items():
        labels = {
            column.name: column.semantic_type
            for column in getattr(table, "columns", [])
            if getattr(column, "semantic_type", None)
        }
        if labels:
            by_table[table_name] = labels
    return by_table
