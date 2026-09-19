"""Database Catalog models.

Core Pydantic models for representing enriched database catalog information.
These models are independent from warp's schema models - they represent
the enriched, LLM-augmented view of the database structure.
"""

from collections import Counter
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from warp.domain.schema import DatabaseSchema


class CatalogStatus(StrEnum):
    """Status of the overall catalog."""

    draft = "draft"
    approved = "approved"


class TableReviewStatus(StrEnum):
    """Per-table review status during HITL review."""

    pending = "pending"
    approved = "approved"
    modified = "modified"


class LocalizedText(BaseModel):
    """Multi-language text container.

    Stores text in multiple languages with fallback support.

    Example:
        text = LocalizedText(texts={"en": "Users", "tr": "Kullanıcılar"})
        text.get("tr")  # "Kullanıcılar"
        text.get("de", fallback="en")  # "Users"
    """

    texts: dict[str, str] = Field(default_factory=dict)

    def get(self, lang: str, fallback: str = "en") -> str:
        """Get text in specified language with fallback."""
        return self.texts.get(lang) or self.texts.get(fallback, "")

    def set(self, lang: str, text: str) -> None:
        """Set text for a specific language."""
        self.texts[lang] = text

    @property
    def is_empty(self) -> bool:
        """Check if no translations exist."""
        return not any(v.strip() for v in self.texts.values())

    @property
    def languages(self) -> list[str]:
        """Get list of available languages."""
        return list(self.texts.keys())

    def __str__(self) -> str:
        """Return first available text."""
        for text in self.texts.values():
            if text.strip():
                return text
        return ""


class ForeignKeyInfo(BaseModel):
    """Foreign key relationship information."""

    column: str = Field(..., description="Source column name")
    references_table: str = Field(..., description="Referenced table name")
    references_column: str = Field(..., description="Referenced column name")
    constraint_name: str | None = Field(default=None, description="FK constraint name")


class IndexInfo(BaseModel):
    """Index information."""

    name: str = Field(..., description="Index name")
    columns: list[str] = Field(default_factory=list, description="Indexed columns")
    unique: bool = Field(default=False, description="Is unique index")


class RelationshipInfo(BaseModel):
    """Enriched relationship description between tables."""

    source_column: str = Field(..., description="Source table column")
    target_table: str = Field(..., description="Target table name")
    target_column: str = Field(..., description="Target table column")
    relationship_type: str = Field(
        default="many-to-one",
        description="Relationship type: one-to-one, one-to-many, many-to-one, many-to-many",
    )
    description: LocalizedText = Field(
        default_factory=LocalizedText,
        description="Human-readable relationship description",
    )


class ColumnCatalogEntry(BaseModel):
    """Enriched column catalog entry.

    Contains both raw schema info and LLM-generated descriptions.
    User edits are tracked in `user_overrides` so they survive regeneration.
    """

    name: str = Field(..., description="Column name")
    data_type: str = Field(..., description="SQL data type")
    full_type: str | None = Field(default=None, description="Full SQL type with length/precision")
    description: LocalizedText = Field(
        default_factory=LocalizedText,
        description="Column description (merged from DB comment + LLM)",
    )
    semantic_type: str | None = Field(
        default=None,
        description="Semantic type: email, user_id, amount, status, phone, url, etc.",
    )
    nullable: bool = Field(default=True, description="Is nullable")
    is_primary_key: bool = Field(default=False, description="Is part of primary key")
    is_foreign_key: bool = Field(default=False, description="Is a foreign key column")
    references: str | None = Field(
        default=None,
        description="FK reference in format 'table.column'",
    )
    default_value: str | None = Field(default=None, description="Default value expression")
    tags: list[str] = Field(default_factory=list, description="Column tags")
    sample_values: list[Any] = Field(
        default_factory=list,
        description="Sample values from the database",
    )
    db_comment: str | None = Field(
        default=None,
        description="Original comment from database (COMMENT ON COLUMN)",
    )
    generated_description: LocalizedText | None = Field(
        default=None,
        description="LLM-generated description (before merging)",
    )
    user_overrides: dict[str, Any] = Field(
        default_factory=dict,
        description="User-edited fields preserved across regeneration. "
        "Keys: description, semantic_type, tags",
    )


class TableCatalogEntry(BaseModel):
    """Enriched table catalog entry.

    Contains full table metadata with LLM-generated descriptions.
    User edits are tracked in `user_overrides` so they survive regeneration.
    """

    table_name: str = Field(..., description="Table name")
    description: LocalizedText = Field(
        default_factory=LocalizedText,
        description="Table description (merged from DB comment + LLM)",
    )
    human_name: LocalizedText = Field(
        default_factory=LocalizedText,
        description="Human-readable table name (e.g., 'Users', 'Kullanıcılar')",
    )
    columns: list[ColumnCatalogEntry] = Field(
        default_factory=list,
        description="Column catalog entries",
    )
    primary_key: str | list[str] | None = Field(
        default=None,
        description="Primary key column(s)",
    )
    foreign_keys: list[ForeignKeyInfo] = Field(
        default_factory=list,
        description="Foreign key definitions",
    )
    indexes: list[IndexInfo] = Field(
        default_factory=list,
        description="Index definitions",
    )
    relationships: list[RelationshipInfo] = Field(
        default_factory=list,
        description="Enriched relationship descriptions",
    )
    row_count: int | None = Field(
        default=None,
        description="Approximate row count",
    )
    tags: list[str] = Field(default_factory=list, description="Table tags")
    db_comment: str | None = Field(
        default=None,
        description="Original comment from database (COMMENT ON TABLE)",
    )
    generated_description: LocalizedText | None = Field(
        default=None,
        description="LLM-generated description (before merging)",
    )
    review_status: TableReviewStatus = Field(
        default=TableReviewStatus.pending,
        description="Review status: pending, approved, modified",
    )
    user_overrides: dict[str, Any] = Field(
        default_factory=dict,
        description="User-edited fields preserved across regeneration. "
        "Keys: description, human_name, tags, relationships, columns (nested)",
    )

    def get_column(self, name: str) -> ColumnCatalogEntry | None:
        """Get a column by name."""
        for col in self.columns:
            if col.name == name:
                return col
        return None

    def get_column_names(self) -> list[str]:
        """Get list of all column names."""
        return [col.name for col in self.columns]

    @property
    def pk_column(self) -> str | None:
        """Get primary key column name (first if composite)."""
        if isinstance(self.primary_key, str):
            return self.primary_key
        elif isinstance(self.primary_key, list) and self.primary_key:
            return self.primary_key[0]
        return None


class DatabaseCatalog(BaseModel):
    """Complete enriched catalog for a database.

    Top-level container for all table catalogs with metadata.
    """

    database_name: str = Field(..., description="Database name")
    database_type: str = Field(
        default="postgresql",
        description="Database type: postgresql, mysql",
    )
    description: LocalizedText = Field(
        default_factory=LocalizedText,
        description="Overall database description",
    )
    tables: dict[str, TableCatalogEntry] = Field(
        default_factory=dict,
        description="Table catalogs keyed by table name",
    )
    generated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="When this catalog was generated",
    )
    updated_at: datetime | None = Field(
        default=None,
        description="When this catalog was last updated",
    )
    version: str = Field(default="1.0.0", description="Catalog version")
    languages: list[str] = Field(
        default_factory=lambda: ["en"],
        description="Languages available in this catalog",
    )
    llm_provider: str | None = Field(
        default=None,
        description="LLM provider used for generation (e.g., openai, anthropic)",
    )
    llm_model: str | None = Field(
        default=None,
        description="LLM model used for generation",
    )
    status: CatalogStatus = Field(
        default=CatalogStatus.draft,
        description="Catalog status: draft or approved",
    )

    def get_table(self, name: str) -> TableCatalogEntry | None:
        """Get a table catalog entry by name."""
        return self.tables.get(name)

    def get_table_names(self) -> list[str]:
        """Get list of all table names."""
        return list(self.tables.keys())

    @property
    def table_count(self) -> int:
        """Get total number of tables."""
        return len(self.tables)

    @property
    def all_tables_approved(self) -> bool:
        """Check if all tables have been approved or modified."""
        return all(
            t.review_status in (TableReviewStatus.approved, TableReviewStatus.modified)
            for t in self.tables.values()
        )

    @property
    def review_summary(self) -> dict[str, int]:
        """Count tables by review status."""
        counts = Counter(t.review_status.value for t in self.tables.values())
        return dict(counts)


class CatalogIndexEntry(BaseModel):
    """Single entry in the catalog index."""

    database_name: str
    database_type: str = "postgresql"
    table_count: int = 0
    languages: list[str] = Field(default_factory=lambda: ["en"])
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime | None = None
    file_path: str = ""
    version: str = "1.0.0"
    status: str = Field(default="draft", description="Catalog status: draft or approved")


class CatalogIndex(BaseModel):
    """Index for all stored catalogs.

    Used by CatalogFileStore to track available catalogs.
    """

    catalogs: dict[str, CatalogIndexEntry] = Field(
        default_factory=dict,
        description="Catalog entries keyed by database name",
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


def structural_catalog(
    database_name: str, schema: DatabaseSchema, database_type: str = ""
) -> DatabaseCatalog:
    """A catalog carrying only what the database itself already said.

    Types, primary keys, foreign keys and nullability are *mechanical* facts:
    the engine's own catalog has them, exactly, for nothing. Descriptions,
    human names and semantic types are not — those need a model and a reviewer.

    The two used to travel together, so the mechanical half was hostage to the
    semantic one: with no catalog analysis run, `x-llm-context` carried nothing
    at all, and a consumer could not see that `musteri_id` points at another
    table until somebody had paid for an LLM pass over every table and approved
    the result. On a schema with a couple of thousand tables that is weeks.

    What this builds stands in for the catalog when there is no approved one.
    It is deliberately *not* marked approved, and it carries no semantic types,
    so nothing that gates on review — masking above all — can be satisfied by
    it. It only makes structure available on day one.
    """
    tables: dict[str, TableCatalogEntry] = {}
    for table_name, table in schema.tables.items():
        references = {
            fk.column: f"{fk.references_table}.{fk.references_column}" for fk in table.foreign_keys
        }
        key_columns = set(table.key_columns)
        tables[table_name] = TableCatalogEntry(
            table_name=table_name,
            columns=[
                ColumnCatalogEntry(
                    name=column.name,
                    data_type=column.type,
                    nullable=column.nullable,
                    is_primary_key=column.name in key_columns,
                    is_foreign_key=column.name in references,
                    references=references.get(column.name),
                )
                for column in table.columns
            ],
            primary_key=table.primary_key,
        )
    return DatabaseCatalog(
        database_name=database_name,
        database_type=database_type,
        tables=tables,
    )
