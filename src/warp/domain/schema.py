"""Schema models for representing database table structures."""

from typing import Any

from pydantic import BaseModel, Field


class ColumnSchema(BaseModel):
    """Represents a database column."""

    name: str
    type: str
    full_type: str | None = None
    nullable: bool = True
    default: Any | None = None
    max_length: int | None = None
    precision: int | None = None
    scale: int | None = None
    key: str | None = None
    extra: str | None = None
    udt_name: str | None = None  # PostgreSQL specific

    @property
    def is_auto_generated(self) -> bool:
        """Whether the database fills this column itself.

        MySQL reports ``auto_increment`` in ``extra``; PostgreSQL serials carry
        a ``nextval(...)`` default; identity and computed columns (PostgreSQL,
        SQL Server) are reported with ``identity`` / ``computed`` in ``extra``
        or ``identity`` in the default expression.
        """
        extra = (self.extra or "").lower()
        default = self.default.lower() if isinstance(self.default, str) else ""
        return (
            "auto_increment" in extra
            or "identity" in extra
            or "computed" in extra
            or "nextval" in default
            or "identity" in default
        )


class ForeignKeySchema(BaseModel):
    """Represents a foreign key relationship."""

    column: str
    references_table: str
    references_column: str
    constraint_name: str | None = None


class IndexSchema(BaseModel):
    """Represents a database index."""

    name: str
    columns: list[str]
    unique: bool = False


class TableSchema(BaseModel):
    """Complete schema representation for a database table.

    Contains all metadata needed to generate API endpoints and
    Pydantic models dynamically.
    """

    table_name: str
    columns: list[ColumnSchema] = Field(default_factory=list)
    primary_key: str | list[str] | None = None
    foreign_keys: list[ForeignKeySchema] = Field(default_factory=list)
    indexes: list[IndexSchema] = Field(default_factory=list)

    @property
    def pk_column(self) -> str | None:
        """Get the primary key column name (first one if composite)."""
        if isinstance(self.primary_key, str):
            return self.primary_key
        elif isinstance(self.primary_key, list) and self.primary_key:
            return self.primary_key[0]
        return None

    @property
    def has_composite_pk(self) -> bool:
        """Check if table has a composite primary key."""
        return isinstance(self.primary_key, list) and len(self.primary_key) > 1

    def get_column(self, name: str) -> ColumnSchema | None:
        """Get a column by name."""
        for col in self.columns:
            if col.name == name:
                return col
        return None

    def get_column_names(self) -> list[str]:
        """Get list of all column names."""
        return [col.name for col in self.columns]

    def get_required_columns(self) -> list[str]:
        """Get list of non-nullable columns without defaults."""
        return [
            col.name
            for col in self.columns
            if not col.nullable
            and col.default is None
            and col.name != self.pk_column
            and not col.is_auto_generated
        ]

    def get_insertable_columns(self) -> list[str]:
        """Get columns that can be inserted (excluding auto-generated)."""
        return [col.name for col in self.columns if not col.is_auto_generated]


class DatabaseSchema(BaseModel):
    """Complete schema for a database."""

    database_name: str
    tables: dict[str, TableSchema] = Field(default_factory=dict)

    def get_table(self, name: str) -> TableSchema | None:
        """Get a table schema by name."""
        return self.tables.get(name)

    def get_table_names(self) -> list[str]:
        """Get list of all table names."""
        return list(self.tables.keys())
