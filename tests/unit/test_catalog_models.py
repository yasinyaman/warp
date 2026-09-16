"""Tests for catalog models."""

import json

from warp.catalog.models import (
    CatalogIndex,
    CatalogIndexEntry,
    ColumnCatalogEntry,
    DatabaseCatalog,
    ForeignKeyInfo,
    IndexInfo,
    LocalizedText,
    RelationshipInfo,
    TableCatalogEntry,
)


class TestLocalizedText:
    """Tests for LocalizedText model."""

    def test_create_empty(self):
        text = LocalizedText()
        assert text.texts == {}
        assert text.is_empty
        assert str(text) == ""

    def test_create_with_texts(self):
        text = LocalizedText(texts={"en": "Users", "tr": "Kullanıcılar"})
        assert text.get("en") == "Users"
        assert text.get("tr") == "Kullanıcılar"
        assert not text.is_empty

    def test_get_with_fallback(self):
        text = LocalizedText(texts={"en": "Users"})
        assert text.get("tr") == "Users"  # Falls back to en
        assert text.get("tr", fallback="en") == "Users"
        assert text.get("de", fallback="fr") == ""  # No fallback available

    def test_set(self):
        text = LocalizedText()
        text.set("en", "Hello")
        text.set("tr", "Merhaba")
        assert text.get("en") == "Hello"
        assert text.get("tr") == "Merhaba"

    def test_languages(self):
        text = LocalizedText(texts={"en": "Hello", "tr": "Merhaba", "de": "Hallo"})
        assert set(text.languages) == {"en", "tr", "de"}

    def test_str(self):
        text = LocalizedText(texts={"en": "Users", "tr": "Kullanıcılar"})
        assert str(text) in ("Users", "Kullanıcılar")  # First non-empty value

    def test_is_empty_with_whitespace(self):
        text = LocalizedText(texts={"en": "  ", "tr": ""})
        assert text.is_empty

    def test_serialization(self):
        text = LocalizedText(texts={"en": "Users", "tr": "Kullanıcılar"})
        data = text.model_dump()
        restored = LocalizedText(**data)
        assert restored.get("en") == "Users"
        assert restored.get("tr") == "Kullanıcılar"


class TestColumnCatalogEntry:
    """Tests for ColumnCatalogEntry model."""

    def test_basic_column(self):
        col = ColumnCatalogEntry(
            name="email",
            data_type="varchar",
            nullable=False,
            semantic_type="email",
        )
        assert col.name == "email"
        assert col.data_type == "varchar"
        assert not col.nullable
        assert col.semantic_type == "email"

    def test_column_with_description(self):
        col = ColumnCatalogEntry(
            name="status",
            data_type="varchar",
            description=LocalizedText(texts={"en": "User status", "tr": "Kullanıcı durumu"}),
            sample_values=["active", "inactive", "suspended"],
        )
        assert col.description.get("en") == "User status"
        assert len(col.sample_values) == 3

    def test_foreign_key_column(self):
        col = ColumnCatalogEntry(
            name="user_id",
            data_type="integer",
            is_foreign_key=True,
            references="users.id",
        )
        assert col.is_foreign_key
        assert col.references == "users.id"

    def test_serialization_roundtrip(self):
        col = ColumnCatalogEntry(
            name="id",
            data_type="integer",
            is_primary_key=True,
            description=LocalizedText(texts={"en": "Primary key"}),
            tags=["pk", "auto_increment"],
        )
        data = col.model_dump(mode="json")
        restored = ColumnCatalogEntry(**data)
        assert restored.name == "id"
        assert restored.is_primary_key
        assert restored.description.get("en") == "Primary key"


class TestTableCatalogEntry:
    """Tests for TableCatalogEntry model."""

    def test_basic_table(self):
        table = TableCatalogEntry(
            table_name="users",
            description=LocalizedText(texts={"en": "User accounts"}),
            human_name=LocalizedText(texts={"en": "Users", "tr": "Kullanıcılar"}),
            primary_key="id",
        )
        assert table.table_name == "users"
        assert table.description.get("en") == "User accounts"
        assert table.pk_column == "id"

    def test_composite_pk(self):
        table = TableCatalogEntry(
            table_name="order_items",
            primary_key=["order_id", "product_id"],
        )
        assert table.pk_column == "order_id"

    def test_get_column(self):
        table = TableCatalogEntry(
            table_name="users",
            columns=[
                ColumnCatalogEntry(name="id", data_type="integer"),
                ColumnCatalogEntry(name="email", data_type="varchar"),
            ],
        )
        col = table.get_column("email")
        assert col is not None
        assert col.data_type == "varchar"
        assert table.get_column("nonexistent") is None

    def test_get_column_names(self):
        table = TableCatalogEntry(
            table_name="users",
            columns=[
                ColumnCatalogEntry(name="id", data_type="integer"),
                ColumnCatalogEntry(name="email", data_type="varchar"),
                ColumnCatalogEntry(name="name", data_type="varchar"),
            ],
        )
        assert table.get_column_names() == ["id", "email", "name"]

    def test_with_relationships(self):
        table = TableCatalogEntry(
            table_name="orders",
            relationships=[
                RelationshipInfo(
                    source_column="user_id",
                    target_table="users",
                    target_column="id",
                    relationship_type="many-to-one",
                    description=LocalizedText(texts={"en": "Order belongs to user"}),
                )
            ],
        )
        assert len(table.relationships) == 1
        assert table.relationships[0].target_table == "users"

    def test_full_serialization(self):
        table = TableCatalogEntry(
            table_name="users",
            description=LocalizedText(texts={"en": "Users table"}),
            columns=[
                ColumnCatalogEntry(
                    name="id",
                    data_type="integer",
                    is_primary_key=True,
                ),
            ],
            foreign_keys=[
                ForeignKeyInfo(
                    column="org_id",
                    references_table="organizations",
                    references_column="id",
                ),
            ],
            indexes=[
                IndexInfo(name="idx_email", columns=["email"], unique=True),
            ],
            row_count=1000,
            tags=["auth", "core"],
        )

        data = json.loads(table.model_dump_json())
        restored = TableCatalogEntry(**data)
        assert restored.table_name == "users"
        assert restored.row_count == 1000
        assert len(restored.foreign_keys) == 1
        assert len(restored.indexes) == 1


class TestDatabaseCatalog:
    """Tests for DatabaseCatalog model."""

    def test_basic_catalog(self):
        catalog = DatabaseCatalog(
            database_name="myapp",
            database_type="postgresql",
            languages=["en", "tr"],
        )
        assert catalog.database_name == "myapp"
        assert catalog.table_count == 0
        assert catalog.get_table_names() == []

    def test_with_tables(self):
        catalog = DatabaseCatalog(
            database_name="myapp",
            tables={
                "users": TableCatalogEntry(table_name="users"),
                "orders": TableCatalogEntry(table_name="orders"),
            },
        )
        assert catalog.table_count == 2
        assert set(catalog.get_table_names()) == {"users", "orders"}
        assert catalog.get_table("users") is not None
        assert catalog.get_table("nonexistent") is None

    def test_serialization_roundtrip(self):
        catalog = DatabaseCatalog(
            database_name="testdb",
            database_type="mysql",
            description=LocalizedText(texts={"en": "Test database"}),
            tables={
                "users": TableCatalogEntry(
                    table_name="users",
                    columns=[
                        ColumnCatalogEntry(name="id", data_type="int"),
                    ],
                ),
            },
            languages=["en", "tr"],
            llm_provider="openai",
            llm_model="gpt-4o-mini",
        )

        json_str = catalog.model_dump_json()
        data = json.loads(json_str)
        restored = DatabaseCatalog(**data)

        assert restored.database_name == "testdb"
        assert restored.database_type == "mysql"
        assert restored.table_count == 1
        assert restored.llm_provider == "openai"


class TestCatalogIndex:
    """Tests for CatalogIndex model."""

    def test_empty_index(self):
        index = CatalogIndex()
        assert index.catalogs == {}

    def test_with_entries(self):
        index = CatalogIndex(
            catalogs={
                "db1": CatalogIndexEntry(
                    database_name="db1",
                    table_count=5,
                    languages=["en"],
                ),
                "db2": CatalogIndexEntry(
                    database_name="db2",
                    database_type="mysql",
                    table_count=10,
                ),
            },
        )
        assert len(index.catalogs) == 2
        assert index.catalogs["db1"].table_count == 5
