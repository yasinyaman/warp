"""Sorting utilities for parsing sort parameters."""

from dataclasses import dataclass


@dataclass
class SortField:
    """Represents a single sort field.

    Attributes:
        column: Column name to sort by.
        direction: Sort direction ('asc' or 'desc').
    """

    column: str
    direction: str = "asc"

    def __post_init__(self) -> None:
        """Validate direction."""
        self.direction = self.direction.lower()
        if self.direction not in ("asc", "desc"):
            raise ValueError(f"Invalid sort direction '{self.direction}'. Must be 'asc' or 'desc'")

    def to_tuple(self) -> tuple[str, str]:
        """Convert to tuple format for database adapter."""
        return (self.column, self.direction)


class SortParser:
    """Parses sort query parameter into sort fields.

    Supports multiple sort formats:
    1. Single field: ?sort=name -> ORDER BY name ASC
    2. With direction: ?sort=name:desc -> ORDER BY name DESC
    3. Multiple fields: ?sort=status:asc,created_at:desc -> ORDER BY status ASC, created_at DESC
    4. Prefix notation: ?sort=-created_at,name -> ORDER BY created_at DESC, name ASC

    The prefix notation uses:
    - No prefix or '+' = ascending
    - '-' = descending
    """

    def __init__(self, allowed_columns: list[str] | None = None):
        """Initialize the sort parser.

        Args:
            allowed_columns: Optional list of allowed column names.
                           If None, all columns are allowed.
        """
        self.allowed_columns = set(allowed_columns) if allowed_columns else None

    def parse(self, sort_param: str) -> list[SortField]:
        """Parse sort parameter string into sort fields.

        Args:
            sort_param: Sort parameter string (e.g., "name:asc,created_at:desc")

        Returns:
            List of SortField objects.

        Raises:
            ValueError: If column is not allowed or format is invalid.
        """
        if not sort_param:
            return []

        fields = []
        parts = [p.strip() for p in sort_param.split(",")]

        for part in parts:
            if not part:
                continue

            field = self._parse_field(part)

            # Validate column
            if self.allowed_columns and field.column not in self.allowed_columns:
                raise ValueError(f"Sorting by column '{field.column}' is not allowed")

            fields.append(field)

        return fields

    def _parse_field(self, field_str: str) -> SortField:
        """Parse a single sort field string."""
        # Check for prefix notation
        if field_str.startswith("-"):
            return SortField(column=field_str[1:], direction="desc")
        if field_str.startswith("+"):
            return SortField(column=field_str[1:], direction="asc")

        # Check for colon notation
        if ":" in field_str:
            parts = field_str.split(":", 1)
            return SortField(column=parts[0], direction=parts[1])

        # Default to ascending
        return SortField(column=field_str, direction="asc")


def parse_sort_from_request(
    sort_param: str | None,
    allowed_columns: list[str] | None = None,
    default_sort: list[tuple[str, str]] | None = None,
) -> list[tuple[str, str]]:
    """Convenience function to parse sort parameter from request.

    Args:
        sort_param: Sort parameter string from query string.
        allowed_columns: Optional list of allowed column names.
        default_sort: Default sort if none provided.

    Returns:
        List of (column, direction) tuples.
    """
    if not sort_param:
        return default_sort or []

    parser = SortParser(allowed_columns)
    fields = parser.parse(sort_param)
    return [f.to_tuple() for f in fields]
