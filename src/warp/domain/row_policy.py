"""Row-level security: which rows a caller may see, change or create.

A :class:`RowPolicy` is a per-table conjunction of :class:`RowRule` conditions
attached to an API key. The rules are expressed in the same vocabulary as user
filters, so they can simply be ANDed onto a query — but they are *mandatory*:
a caller cannot widen, drop or override them with query parameters.

Two things use the same rules, and both are needed:

- reads that go through a ``WHERE`` clause (list, export, stream) push the
  conditions into SQL, so the database never returns a row the caller may not
  see;
- reads and writes addressed by primary key have no ``WHERE`` to push into, so
  the row is matched in memory instead, and a row outside the policy is
  reported as missing rather than refused — telling a caller that a record
  exists but is not theirs is itself a disclosure.

Values may be templated against the caller: ``${tenant}`` is the point of the
whole feature, and ``${username}`` covers "only your own rows".
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

# The same operators user filters may use; a rule that could not be written as
# a filter would not be pushable into the WHERE clause.
ROW_OPERATORS = frozenset({"eq", "ne", "gt", "gte", "lt", "lte", "like", "in", "is_null"})

_TEMPLATE = re.compile(r"^\$\{(\w+)\}$")


class PolicyError(ValueError):
    """A row policy that cannot be applied as written."""


@dataclass(frozen=True)
class RowRule:
    """One mandatory ``column <operator> value`` condition.

    ``value`` may be ``${tenant}`` or ``${username}``, resolved against the
    caller when the rule is applied.
    """

    column: str
    operator: str = "eq"
    value: Any = None

    def __post_init__(self) -> None:
        """Reject a rule that could not be applied, at construction time."""
        if not isinstance(self.column, str) or not self.column:
            raise PolicyError(f"Row rule needs a column, got {self.column!r}")
        if self.operator not in ROW_OPERATORS:
            raise PolicyError(
                f"Unknown row-rule operator '{self.operator}' on '{self.column}'. "
                f"Available: {', '.join(sorted(ROW_OPERATORS))}."
            )

    def resolve(self, caller: Mapping[str, Any]) -> tuple[str, str, Any]:
        """``(column, operator, value)`` with any template filled in.

        Raises:
            PolicyError: When the template names something the caller has no
                value for. Refusing is the only safe answer: substituting an
                empty string would silently widen the rule to "rows whose
                tenant is blank".
        """
        value = self.value
        if isinstance(value, str):
            match = _TEMPLATE.match(value)
            if match:
                attribute = match.group(1)
                resolved = caller.get(attribute)
                if resolved is None or resolved == "":
                    raise PolicyError(
                        f"Row rule on '{self.column}' needs the caller's "
                        f"'{attribute}', which is not set on this API key."
                    )
                value = resolved
        if self.operator == "in" and not isinstance(value, list | tuple | set):
            raise PolicyError(f"Row rule on '{self.column}' with 'in' needs a list of values")
        return self.column, self.operator, value

    def permits(self, row: Mapping[str, Any], caller: Mapping[str, Any]) -> bool:
        """Whether ``row`` satisfies this rule.

        Anything that cannot be decided — a missing column, values that do not
        compare — answers False. A row-security check that guesses is worse
        than one that refuses.
        """
        _, operator, expected = self.resolve(caller)
        if self.column not in row:
            return False
        actual = row[self.column]
        if operator == "is_null":
            return (actual is None) is bool(expected)
        if actual is None:
            return False
        if operator == "in":
            return any(_equal(actual, item) for item in expected)
        if operator == "eq":
            return _equal(actual, expected)
        if operator == "ne":
            return not _equal(actual, expected)
        if operator == "like":
            return _like(str(actual), str(expected))
        return _ordered(operator, actual, expected)


@dataclass(frozen=True)
class RowPolicy:
    """The row rules that apply to one caller, by table."""

    rules: Mapping[str, tuple[RowRule, ...]] = field(default_factory=dict)
    caller: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        """Whether this policy restricts nothing at all."""
        return not any(self.rules.values())

    def covers(self, table: str) -> bool:
        """Whether any rule applies to ``table``."""
        return bool(self.rules.get(table))

    def conditions_for(self, table: str) -> list[tuple[str, str, Any]]:
        """The rules for ``table`` as filter tuples, ready to AND into a query."""
        return [rule.resolve(self.caller) for rule in self.rules.get(table, ())]

    def permits(self, table: str, row: Mapping[str, Any] | None) -> bool:
        """Whether ``row`` is one this caller may see or change."""
        if row is None:
            return False
        return all(rule.permits(row, self.caller) for rule in self.rules.get(table, ()))

    def columns_for(self, table: str) -> set[str]:
        """Columns the rules read, which a projection must therefore keep."""
        return {rule.column for rule in self.rules.get(table, ())}

    def apply(
        self, table: str, filters: Sequence[tuple[str, str, Any]] | None
    ) -> list[tuple[str, str, Any]]:
        """AND this policy's conditions onto a caller's filters.

        Policy conditions go last so they are the final word, and the caller's
        own filters can only ever narrow the result further.
        """
        return [*(filters or []), *self.conditions_for(table)]


EMPTY_POLICY = RowPolicy()
"""No restriction — what an unrestricted key or a disabled auth setup gets."""


def build_policy(
    rules: Mapping[str, Iterable[Mapping[str, Any] | RowRule]] | None,
    caller: Mapping[str, Any] | None = None,
) -> RowPolicy:
    """Build a policy from configuration.

    Raises:
        PolicyError: On a malformed rule, so a misconfigured policy fails at
            startup rather than silently letting every row through.
    """
    if not rules:
        return RowPolicy(rules={}, caller=dict(caller or {}))
    built: dict[str, tuple[RowRule, ...]] = {}
    for table, raw_rules in rules.items():
        entries: list[RowRule] = []
        for raw in raw_rules:
            if isinstance(raw, RowRule):
                entries.append(raw)
                continue
            if not isinstance(raw, Mapping):
                raise PolicyError(f"Row rule for '{table}' must be an object, got {raw!r}")
            entries.append(
                RowRule(
                    column=str(raw.get("column", "")),
                    operator=str(raw.get("operator", raw.get("op", "eq"))),
                    value=raw.get("value"),
                )
            )
        if entries:
            built[str(table)] = tuple(entries)
    return RowPolicy(rules=built, caller=dict(caller or {}))


# --- comparison helpers ------------------------------------------------------------


def _equal(actual: Any, expected: Any) -> bool:
    """Equality that survives the type a driver happens to return.

    A tenant read back as ``Decimal('7')`` still matches a configured ``7``,
    and a ``UUID`` still matches its text form.
    """
    # A boolean compares equal to 0/1 on purpose: MySQL stores booleans as
    # tinyint(1) and hands them back as bool, so a rule written `active = 1`
    # has to keep matching the rows the SQL path already selected.
    if actual == expected:
        return True
    if isinstance(actual, int | float) and isinstance(expected, int | float):
        return float(actual) == float(expected)
    try:
        if isinstance(expected, str) and str(actual) == expected:
            return True
        if isinstance(actual, str) and actual == str(expected):
            return True
    except Exception:  # pragma: no cover - str() on an exotic driver type
        return False
    return False


def _ordered(operator: str, actual: Any, expected: Any) -> bool:
    """``gt``/``gte``/``lt``/``lte``; incomparable values answer False."""
    try:
        if operator == "gt":
            return bool(actual > expected)
        if operator == "gte":
            return bool(actual >= expected)
        if operator == "lt":
            return bool(actual < expected)
        return bool(actual <= expected)
    except TypeError:
        return False


def _like(actual: str, pattern: str) -> bool:
    """SQL ``LIKE`` semantics: ``%`` is any run, ``_`` is one character."""
    regex = "".join(
        ".*" if char == "%" else "." if char == "_" else re.escape(char) for char in pattern
    )
    return re.match(f"^{regex}$", actual, re.DOTALL) is not None
