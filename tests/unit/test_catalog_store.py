"""Tests for CatalogFileStore."""

import json
import tempfile
from pathlib import Path

import pytest

from warp.catalog.models import (
    ColumnCatalogEntry,
    DatabaseCatalog,
    LocalizedText,
    TableCatalogEntry,
)
from warp.catalog.store import CatalogFileStore, validate_catalog_name
from warp.core.exceptions import InvalidCatalogNameError


@pytest.fixture
def temp_dir():
    """Create a temporary directory for catalog storage."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def store(temp_dir):
    """Create a CatalogFileStore with temp directory."""
    return CatalogFileStore(temp_dir)


@pytest.fixture
def sample_catalog():
    """Create a sample DatabaseCatalog for testing."""
    return DatabaseCatalog(
        database_name="testdb",
        database_type="postgresql",
        description=LocalizedText(texts={"en": "Test database", "tr": "Test veritabanı"}),
        tables={
            "users": TableCatalogEntry(
                table_name="users",
                description=LocalizedText(texts={"en": "User accounts"}),
                human_name=LocalizedText(texts={"en": "Users", "tr": "Kullanıcılar"}),
                columns=[
                    ColumnCatalogEntry(
                        name="id",
                        data_type="integer",
                        is_primary_key=True,
                        description=LocalizedText(texts={"en": "Primary key"}),
                    ),
                    ColumnCatalogEntry(
                        name="email",
                        data_type="varchar",
                        semantic_type="email",
                        description=LocalizedText(texts={"en": "User email"}),
                        sample_values=["alice@example.com", "bob@example.com"],
                    ),
                ],
                primary_key="id",
                row_count=1000,
            ),
            "orders": TableCatalogEntry(
                table_name="orders",
                description=LocalizedText(texts={"en": "Customer orders"}),
                columns=[
                    ColumnCatalogEntry(name="id", data_type="integer", is_primary_key=True),
                    ColumnCatalogEntry(
                        name="user_id",
                        data_type="integer",
                        is_foreign_key=True,
                        references="users.id",
                    ),
                ],
                primary_key="id",
            ),
        },
        languages=["en", "tr"],
    )


class TestCatalogFileStore:
    """Tests for basic save/load operations."""

    def test_save_json(self, store, sample_catalog):
        path = store.save(sample_catalog, format="json")

        assert path.exists()
        assert path.suffix == ".json"

        # Verify JSON content
        with open(path) as f:
            data = json.load(f)
        assert data["database_name"] == "testdb"
        assert "users" in data["tables"]

    def test_save_yaml(self, store, sample_catalog):
        path = store.save(sample_catalog, format="yaml")

        assert path.exists()
        assert path.suffix == ".yaml"

    def test_load_json(self, store, sample_catalog):
        store.save(sample_catalog, format="json")
        loaded = store.load("testdb")

        assert loaded is not None
        assert loaded.database_name == "testdb"
        assert loaded.table_count == 2
        assert loaded.get_table("users") is not None

    def test_load_yaml(self, store, sample_catalog):
        store.save(sample_catalog, format="yaml")
        loaded = store.load("testdb")

        assert loaded is not None
        assert loaded.database_name == "testdb"

    def test_load_nonexistent(self, store):
        result = store.load("nonexistent")
        assert result is None

    def test_load_or_raise(self, store, sample_catalog):
        from warp.core.exceptions import CatalogNotFoundError

        store.save(sample_catalog)
        loaded = store.load_or_raise("testdb")
        assert loaded.database_name == "testdb"

        with pytest.raises(CatalogNotFoundError):
            store.load_or_raise("nonexistent")

    def test_list_catalogs(self, store, sample_catalog):
        assert store.list_catalogs() == []

        store.save(sample_catalog)
        assert store.list_catalogs() == ["testdb"]

        # Save another
        catalog2 = DatabaseCatalog(database_name="otherdb")
        store.save(catalog2)
        assert set(store.list_catalogs()) == {"otherdb", "testdb"}

    def test_delete(self, store, sample_catalog):
        store.save(sample_catalog)
        assert store.load("testdb") is not None

        result = store.delete("testdb")
        assert result is True
        assert store.load("testdb") is None

        result = store.delete("nonexistent")
        assert result is False

    def test_index_updated(self, store, sample_catalog):
        store.save(sample_catalog)

        index = store.get_index()
        assert "testdb" in index.catalogs
        entry = index.catalogs["testdb"]
        assert entry.database_name == "testdb"
        assert entry.table_count == 2
        assert entry.database_type == "postgresql"

    def test_preserves_data_integrity(self, store, sample_catalog):
        """Verify all data survives save/load roundtrip."""
        store.save(sample_catalog)
        loaded = store.load("testdb")

        assert loaded is not None

        # Check table data
        users = loaded.get_table("users")
        assert users is not None
        assert users.row_count == 1000
        assert users.description.get("en") == "User accounts"
        assert users.human_name.get("tr") == "Kullanıcılar"

        # Check column data
        email_col = users.get_column("email")
        assert email_col is not None
        assert email_col.semantic_type == "email"
        assert email_col.description.get("en") == "User email"
        assert len(email_col.sample_values) == 2

        # Check FK column
        orders = loaded.get_table("orders")
        assert orders is not None
        user_id_col = orders.get_column("user_id")
        assert user_id_col is not None
        assert user_id_col.is_foreign_key
        assert user_id_col.references == "users.id"


class TestCrossReference:
    """Tests for cross-reference functionality."""

    def test_find_similar_tables(self, store):
        # Save two catalogs with similar table names
        cat1 = DatabaseCatalog(
            database_name="db1",
            tables={
                "users": TableCatalogEntry(
                    table_name="users",
                    description=LocalizedText(texts={"en": "User accounts"}),
                ),
            },
        )
        cat2 = DatabaseCatalog(
            database_name="db2",
            tables={
                "app_users": TableCatalogEntry(
                    table_name="app_users",
                    description=LocalizedText(texts={"en": "Application users"}),
                ),
            },
        )

        store.save(cat1)
        store.save(cat2)

        # Find similar to "users" excluding db1
        results = store.find_similar_tables("users", exclude_db="db1")
        assert len(results) >= 1
        db_names = [r[0] for r in results]
        assert "db2" in db_names

    def test_find_similar_columns(self, store, sample_catalog):
        store.save(sample_catalog)

        results = store.find_similar_columns("email", exclude_db=None)
        assert len(results) >= 1
        # Should find email column in testdb.users
        found = any(col.semantic_type == "email" for _, _, col in results)
        assert found

    def test_build_cross_reference_context(self, store, sample_catalog):
        store.save(sample_catalog)

        context = store.build_cross_reference_context(
            table_name="users",
            column_names=["email", "status"],
            exclude_db="other_db",
        )

        # Should contain some reference text
        assert isinstance(context, str)
        assert len(context) > 0

    def test_no_cross_references(self, store):
        context = store.build_cross_reference_context(
            table_name="nonexistent",
            column_names=["col1"],
        )
        assert "No previous catalog data" in context


class TestNameNormalization:
    """Tests for name normalization in cross-reference."""

    @pytest.mark.parametrize(
        "input_name,expected",
        [
            ("users", "user"),
            ("tbl_users", "user"),
            ("user_id", "user"),
            ("categories", "category"),
            ("addresses", "address"),
            ("order_items", "order_item"),
        ],
    )
    def test_normalize_name(self, input_name, expected):
        result = CatalogFileStore._normalize_name(input_name)
        assert result == expected

    @pytest.mark.parametrize(
        "name1,name2,expected",
        [
            ("user", "user", True),
            ("user", "users", True),  # "user" is contained in "users"
            ("abc", "abcdef", True),  # Contains
            ("ab", "xy", False),  # Too short for contains
        ],
    )
    def test_names_are_similar(self, name1, name2, expected):
        result = CatalogFileStore._names_are_similar(name1, name2)
        assert result == expected


class TestPathSafety:
    """Catalog names are directory names: traversal and collisions must be refused."""

    @pytest.mark.parametrize(
        "bad", ["..", "../x", "a/b", "a\\b", "_index", "", "a b", ".hidden", "x" * 65, "ünïcode"]
    )
    def test_invalid_names_rejected(self, bad):
        with pytest.raises(InvalidCatalogNameError):
            validate_catalog_name(bad)

    @pytest.mark.parametrize("good", ["testdb", "primary_db", "db-1", "A9", "x" * 64])
    def test_valid_names_accepted(self, good):
        assert validate_catalog_name(good) == good

    def test_delete_cannot_escape_root(self, temp_dir):
        root = temp_dir / "catalogs"
        store = CatalogFileStore(root)
        sibling = temp_dir / "sibling"
        sibling.mkdir()
        (sibling / "keep.txt").write_text("x")

        for name in ("..", "../sibling", "../../"):
            with pytest.raises(InvalidCatalogNameError):
                store.delete(name)

        assert (sibling / "keep.txt").exists()
        assert root.exists()

    def test_load_and_save_reject_traversal(self, store, sample_catalog):
        with pytest.raises(InvalidCatalogNameError):
            store.load("../../etc")
        with pytest.raises(InvalidCatalogNameError):
            store.load_or_raise("..")

        evil = sample_catalog.model_copy(update={"database_name": "../evil"})
        with pytest.raises(InvalidCatalogNameError):
            store.save(evil)
        assert not (store.base_path.parent / "evil").exists()
        assert store.list_catalogs() == []
