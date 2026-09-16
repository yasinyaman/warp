"""Database comment value objects (pure)."""

from dataclasses import dataclass, field


@dataclass
class TableComments:
    """Comments for a single table."""

    table_name: str
    table_comment: str | None = None
    column_comments: dict[str, str] = field(default_factory=dict)
