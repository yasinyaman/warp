"""
Filtering utilities for parsing query parameters into filter conditions.
"""
import re
from dataclasses import dataclass
from typing import Any


@dataclass
class FilterCondition:
    """
    Represents a single filter condition.

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
    """
    Parses query parameters into filter conditions.

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

    OPERATORS = {
        "eq", "ne", "gt", "gte", "lt", "lte", "like", "in", "is_null"
    }

    # Pattern for filter[column] or filter[column][operator]
    FILTER_PATTERN = re.compile(r"filter\[(\w+)\](?:\[(\w+)\])?")

    def __init__(self, allowed_columns: list[str] | None = None):
        """
        Initialize the filter parser.

        Args:
            allowed_columns: Optional list of allowed column names.
                           If None, all columns are allowed.
        """
        self.allowed_columns = set(allowed_columns) if allowed_columns else None

    def parse(self, query_params: dict[str, str]) -> list[FilterCondition]:
        """
        Parse query parameters into filter conditions.

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
                    f"Invalid operator '{operator}'. "
                    f"Allowed: {', '.join(self.OPERATORS)}"
                )

            # Parse value based on operator
            parsed_value = self._parse_value(value, operator)

            filters.append(FilterCondition(
                column=column,
                operator=operator,
                value=parsed_value
            ))

        return filters

    def _parse_value(self, value: str, operator: str) -> Any:
        """Parse and convert filter value based on operator."""
        if operator == "is_null":
            return value.lower() in ("true", "1", "yes")

        if operator == "in":
            # Split comma-separated values
            values = [v.strip() for v in value.split(",")]
            return [self._convert_value(v) for v in values]

        return self._convert_value(value)

    @staticmethod
    def _convert_value(value: str) -> Any:
        """
        Attempt to convert string value to appropriate Python type.

        Tries to convert to: int, float, bool, or keeps as string.
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
    allowed_columns: list[str] | None = None
) -> list[tuple[str, str, Any]]:
    """
    Convenience function to parse filters from request query params.

    Args:
        query_params: Dictionary of query parameters.
        allowed_columns: Optional list of allowed column names.

    Returns:
        List of (column, operator, value) tuples.
    """
    parser = FilterParser(allowed_columns)
    conditions = parser.parse(query_params)
    return [c.to_tuple() for c in conditions]
