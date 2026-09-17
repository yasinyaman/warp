"""Filtering utilities for parsing query parameters into filter conditions."""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any
from uuid import UUID


@dataclass
class FilterCondition:
    """Represents a single filter condition.

    Attributes:
        column: Column name to filter on.
        operator: Filter operator (eq, ne, gt, gte, lt, lte, like, in, is_null).
        value: Value to filter against.
    """

    column: str
    operator: str
    value: Any

    def to_tuple(self) -> tuple[str, str, Any]:
        """Convert to tuple format for database adapter."""
        return (self.column, self.operator, self.value)


class FilterParser:
    """Parses query parameters into filter conditions.

    Supports multiple filter formats:
    1. Simple: ?filter[status]=active  -> status = 'active'
    2. With operator: ?filter[price][gte]=100 -> price >= 100
    3. Multiple values: ?filter[status][in]=active,pending -> status IN ('active', 'pending')
    4. Null check: ?filter[deleted_at][is_null]=true -> deleted_at IS NULL

    Supported operators:
    - eq: Equal (default)
    - ne: Not equal
    - gt: Greater than
    - gte: Greater than or equal
    - lt: Less than
    - lte: Less than or equal
    - like: LIKE pattern matching (use % for wildcards)
    - in: IN list of values
    - is_null: IS NULL check (value should be 'true' or 'false')
    """

    OPERATORS = {"eq", "ne", "gt", "gte", "lt", "lte", "like", "in", "is_null"}

    # Pattern for filter[column] or filter[column][operator]
    FILTER_PATTERN = re.compile(r"filter\[(\w+)\](?:\[(\w+)\])?")

    _TRUE = frozenset({"true", "1", "yes", "t", "y"})
    _FALSE = frozenset({"false", "0", "no", "f", "n"})
    # ISO-8601 text -> the objects the drivers expect for typed parameters.
    _PARSERS: dict[str, Any] = {
        "datetime": datetime.fromisoformat,
        "date": date.fromisoformat,
        "time": time.fromisoformat,
        "uuid": UUID,
    }

    def __init__(
        self,
        allowed_columns: list[str] | None = None,
        column_kinds: Mapping[str, str] | None = None,
    ):
        """Initialize the filter parser.

        Args:
            allowed_columns: Optional list of allowed column names.
                           If None, all columns are allowed.
            column_kinds: Optional mapping of column name to a type kind
                (``int``, ``float``, ``bool``, ``str``...). When a column's kind
                is known, its filter value is converted to exactly that kind
                (``"007"`` stays ``"007"`` for a text column; ``"abc"`` is an
                error for an integer column). Columns without a kind fall back
                to shape-based guessing.
        """
        self.allowed_columns = set(allowed_columns) if allowed_columns else None
        self.column_kinds = dict(column_kinds) if column_kinds else {}

    def parse(self, query_params: dict[str, str]) -> list[FilterCondition]:
        """Parse query parameters into filter conditions.

        Args:
            query_params: Dictionary of query parameters.

        Returns:
            List of FilterCondition objects.

        Raises:
            ValueError: If column is not allowed or operator is invalid.
        """
        filters = []

        for key, value in query_params.items():
            match = self.FILTER_PATTERN.match(key)
            if not match:
                continue

            column = match.group(1)
            operator = match.group(2) or "eq"

            # Validate column
            if self.allowed_columns and column not in self.allowed_columns:
                raise ValueError(f"Filter on column '{column}' is not allowed")

            # Validate operator
            if operator not in self.OPERATORS:
                raise ValueError(
                    f"Invalid operator '{operator}'. Allowed: {', '.join(self.OPERATORS)}"
                )

            # Parse value based on operator and (when known) the column's kind
            parsed_value = self._parse_value(value, operator, column)

            filters.append(FilterCondition(column=column, operator=operator, value=parsed_value))

        return filters

    def parse_conditions(self, conditions: Iterable[tuple[str, str, Any]]) -> list[FilterCondition]:
        """Validate already-structured ``(column, operator, value)`` triples.

        This is the JSON-body counterpart of ``parse``: values may arrive as
        typed JSON (``int``/``float``/``bool``/``list``) and are passed through,
        while string values are coerced to the column's kind exactly like
        query-string values. ``in`` requires a list.

        Raises:
            ValueError: On an unknown column/operator or an unconvertible value.
        """
        filters = []
        for column, operator, value in conditions:
            if self.allowed_columns and column not in self.allowed_columns:
                raise ValueError(f"Filter on column '{column}' is not allowed")
            if operator not in self.OPERATORS:
                raise ValueError(
                    f"Invalid operator '{operator}'. Allowed: {', '.join(sorted(self.OPERATORS))}"
                )
            parsed = self._coerce_structured(value, operator, column)
            filters.append(FilterCondition(column=column, operator=operator, value=parsed))
        return filters

    def _coerce_structured(self, value: Any, operator: str, column: str) -> Any:
        """Coerce a JSON value for ``operator`` on ``column``."""
        if operator == "is_null":
            return value.lower() in self._TRUE if isinstance(value, str) else bool(value)
        if operator == "like":
            return str(value)
        if operator == "in":
            if not isinstance(value, list | tuple):
                raise ValueError(f"Filter 'in' on column '{column}' needs a list of values")
            return [self._coerce_scalar(v, column) for v in value]
        return self._coerce_scalar(value, column)

    def _coerce_scalar(self, value: Any, column: str) -> Any:
        """Strings follow the column's kind; typed JSON values pass through."""
        if isinstance(value, str):
            return self._convert_value(value, self.column_kinds.get(column), column)
        return value

    def _parse_value(self, value: str, operator: str, column: str = "") -> Any:
        """Parse and convert filter value based on operator and column kind."""
        if operator == "is_null":
            return value.lower() in ("true", "1", "yes")

        if operator == "like":
            # A LIKE pattern is always text, whatever the column type.
            return value

        kind = self.column_kinds.get(column)

        if operator == "in":
            # Split comma-separated values
            values = [v.strip() for v in value.split(",")]
            return [self._convert_value(v, kind, column) for v in values]

        return self._convert_value(value, kind, column)

    @classmethod
    def _convert_value(cls, value: str, kind: str | None = None, column: str = "") -> Any:
        """Convert a filter value to the column's kind, or guess when unknown.

        Raises:
            ValueError: If the value cannot be converted to a known kind.
        """
        if kind is None:
            return cls._convert_untyped(value)

        if kind == "int":
            try:
                return int(value)
            except ValueError:
                raise ValueError(
                    f"Filter value for column '{column}' must be an integer, got {value!r}"
                ) from None

        if kind == "float":
            try:
                return float(value)
            except ValueError:
                raise ValueError(
                    f"Filter value for column '{column}' must be a number, got {value!r}"
                ) from None

        if kind == "bool":
            lowered = value.lower()
            if lowered in cls._TRUE:
                return True
            if lowered in cls._FALSE:
                return False
            raise ValueError(f"Filter value for column '{column}' must be a boolean, got {value!r}")

        parser = cls._PARSERS.get(kind)
        if parser is not None:
            try:
                return parser(value)
            except ValueError:
                raise ValueError(
                    f"Filter value for column '{column}' must be a valid {kind}, got {value!r}"
                ) from None

        # str/json/bytes/list/date...: pass the text through untouched; the
        # database (with a bound parameter) performs any further conversion.
        return value

    @staticmethod
    def _convert_untyped(value: str) -> Any:
        """Guess a Python type from the shape of the text (legacy behaviour).

        Tries to convert to: None, bool, int, float, or keeps as string.
        """
        # Check for null/None
        if value.lower() in ("null", "none"):
            return None

        # Check for boolean
        if value.lower() in ("true", "false"):
            return value.lower() == "true"

        # Try integer
        try:
            return int(value)
        except ValueError:
            pass

        # Try float
        try:
            return float(value)
        except ValueError:
            pass

        # Keep as string
        return value


def parse_filters_from_request(
    query_params: dict[str, str],
    allowed_columns: list[str] | None = None,
    column_kinds: Mapping[str, str] | None = None,
) -> list[tuple[str, str, Any]]:
    """Convenience function to parse filters from request query params.

    Args:
        query_params: Dictionary of query parameters.
        allowed_columns: Optional list of allowed column names.
        column_kinds: Optional column name -> kind mapping for typed coercion.

    Returns:
        List of (column, operator, value) tuples.

    Raises:
        ValueError: On an unknown column/operator or an unconvertible value.
    """
    parser = FilterParser(allowed_columns, column_kinds)
    conditions = parser.parse(query_params)
    return [c.to_tuple() for c in conditions]


def parse_filter_conditions(
    conditions: Iterable[tuple[str, str, Any]],
    allowed_columns: list[str] | None = None,
    column_kinds: Mapping[str, str] | None = None,
) -> list[tuple[str, str, Any]]:
    """Validate structured filter triples (e.g. from a JSON body).

    Args:
        conditions: ``(column, operator, value)`` triples.
        allowed_columns: Optional list of allowed column names.
        column_kinds: Optional column name -> kind mapping for typed coercion.

    Returns:
        List of ``(column, operator, value)`` tuples with coerced values.

    Raises:
        ValueError: On an unknown column/operator or an unconvertible value.
    """
    parser = FilterParser(allowed_columns, column_kinds)
    return [c.to_tuple() for c in parser.parse_conditions(conditions)]
