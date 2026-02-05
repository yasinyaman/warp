"""
Schema models for representing database table structures.
"""
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, Field


class ColumnSchema(BaseModel):
    """Represents a database column."""
    name: str
    type: str
    full_type: Optional[str] = None
    nullable: bool = True
    default: Optional[Any] = None
    max_length: Optional[int] = None
    precision: Optional[int] = None
    scale: Optional[int] = None
    key: Optional[str] = None
    extra: Optional[str] = None
    udt_name: Optional[str] = None  # PostgreSQL specific


class ForeignKeySchema(BaseModel):
    """Represents a foreign key relationship."""
    column: str
    references_table: str
    references_column: str
    constraint_name: Optional[str] = None


class IndexSchema(BaseModel):
    """Represents a database index."""
    name: str
    columns: List[str]
    unique: bool = False


class TableSchema(BaseModel):
    """
    Complete schema representation for a database table.

    Contains all metadata needed to generate API endpoints and
    Pydantic models dynamically.
    """
    table_name: str
    columns: List[ColumnSchema] = Field(default_factory=list)
    primary_key: Optional[Union[str, List[str]]] = None
    foreign_keys: List[ForeignKeySchema] = Field(default_factory=list)
    indexes: List[IndexSchema] = Field(default_factory=list)

    @property
    def pk_column(self) -> Optional[str]:
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

    def get_column(self, name: str) -> Optional[ColumnSchema]:
        """Get a column by name."""
        for col in self.columns:
            if col.name == name:
                return col
        return None

    def get_column_names(self) -> List[str]:
        """Get list of all column names."""
        return [col.name for col in self.columns]

    def get_required_columns(self) -> List[str]:
        """Get list of non-nullable columns without defaults."""
        return [
            col.name for col in self.columns
            if not col.nullable and col.default is None
            and col.name != self.pk_column
            and "auto_increment" not in (col.extra or "").lower()
            and "nextval" not in (col.default or "").lower()
        ]

    def get_insertable_columns(self) -> List[str]:
        """Get columns that can be inserted (excluding auto-generated)."""
        excluded = set()
        pk = self.pk_column

        for col in self.columns:
            # Exclude auto-increment columns
            if col.extra and "auto_increment" in col.extra.lower():
                excluded.add(col.name)
            # Exclude serial/identity columns in PostgreSQL
            if col.default and ("nextval" in col.default.lower() or "identity" in col.default.lower()):
                excluded.add(col.name)

        return [col.name for col in self.columns if col.name not in excluded]


class DatabaseSchema(BaseModel):
    """Complete schema for a database."""
    database_name: str
    tables: Dict[str, TableSchema] = Field(default_factory=dict)

    def get_table(self, name: str) -> Optional[TableSchema]:
        """Get a table schema by name."""
        return self.tables.get(name)

    def get_table_names(self) -> List[str]:
        """Get list of all table names."""
        return list(self.tables.keys())
