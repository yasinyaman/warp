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


#: Bytes of digest the ``hash`` strategy emits, as hex. 128 bits: at the 48 it
#: used to emit, ten million distinct addresses collide with better than one
#: chance in six, which would corrupt the very joins the strategy exists for.
HASH_DIGEST_BYTES = 16

#: BLAKE2b takes at most this much key material directly.
_MAX_KEY_BYTES = 64


def _pseudonym(text: str, key: bytes) -> str:
    """A keyed pseudonym for one value.

    Keyed BLAKE2b rather than a bare digest, because a bare digest of
    enumerable data is not a mask. An email falls to a wordlist; a TCKN is
    eleven digits with a check rule, so the valid space is about a billion and
    a laptop walks all of it. The key is what moves the attack from "anyone
    holding the export" to "anyone holding the key".

    Keyed BLAKE2 rather than HMAC because it absorbs the key into the initial
    block instead of compressing twice: measured at 0.40 µs against the 0.38 µs
    of the unkeyed SHA-256 this replaces, where HMAC-SHA256 costs 1.38 µs. On
    the export path — one call per masked cell, unbounded rows — that
    difference is the whole argument, and it comes out free.
    """
    if len(key) > _MAX_KEY_BYTES:
        # Compressed rather than truncated, so a long passphrase keeps all of
        # its entropy instead of only its first 64 bytes.
        key = hashlib.blake2b(key, digest_size=_MAX_KEY_BYTES).digest()
    return hashlib.blake2b(text.encode("utf-8"), key=key, digest_size=HASH_DIGEST_BYTES).hexdigest()


def mask_value(value: Any, strategy: str, key: bytes | None = None) -> Any:
    """Apply one strategy to one value.

    ``None`` stays ``None`` — masking a value that is not there would invent
    the appearance of data.

    Args:
        value: The cell to mask.
        strategy: One of :data:`STRATEGIES`.
        key: Required by ``hash`` and ignored by everything else. Threaded from
            the policy rather than read from configuration, because this module
            is pure domain and has no way to reach a secret.

    Raises:
        MaskingError: On an unknown strategy, or on ``hash`` with no key. The
            refusal lives here so that "never an unkeyed digest" is a property
            of the code rather than of whoever wired it up.
    """
    if value is None:
        return None
    if strategy == "null":
        return None
    text = str(value)
    if strategy == "redact":
        return REDACTED
    if strategy == "hash":
        if not key:
            raise MaskingError(
                "The 'hash' strategy needs a key: set masking.hash_secret. "
                "An unkeyed digest of an email or a national id is reversible "
                "by enumeration, so there is no safe default to fall back to."
            )
        return _pseudonym(text, key)
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
    #: Keys the ``hash`` strategy. ``repr=False`` because this dataclass is
    #: nested inside ``CatalogMasking``, and one ``logger.debug(masking)``
    #: anywhere would otherwise print the secret.
    hash_key: bytes | None = field(default=None, repr=False)

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


def mask_row(
    row: Mapping[str, Any], masks: Mapping[str, str], hash_key: bytes | None = None
) -> dict[str, Any]:
    """A copy of ``row`` with the masked columns replaced."""
    if not masks:
        return dict(row)
    return {
        column: mask_value(value, masks[column], hash_key) if column in masks else value
        for column, value in row.items()
    }


def mask_rows(
    rows: Iterable[Mapping[str, Any]],
    masks: Mapping[str, str],
    hash_key: bytes | None = None,
) -> list[dict[str, Any]]:
    """``mask_row`` over a batch."""
    return [mask_row(row, masks, hash_key) for row in rows]


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
    def hash_key(self) -> bytes | None:
        """The policy's key, so a route reaches through one object, not two."""
        return self.policy.hash_key

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
