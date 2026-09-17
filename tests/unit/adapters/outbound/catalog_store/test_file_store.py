"""Tests for CatalogFileStore."""

import json
import tempfile
from pathlib import Path

import pytest

from warp.adapters.outbound.catalog_store.file_store import CatalogFileStore
from warp.application.services.catalog_review import CatalogReviewService
from warp.domain.catalog import (
    ColumnCatalogEntry,
    DatabaseCatalog,
    LocalizedText,
    TableCatalogEntry,
)
from warp.domain.catalog_naming import validate_catalog_name
from warp.domain.errors import InvalidCatalogNameError


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
        from warp.domain.errors import CatalogNotFoundError

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


class TestDefaultFormat:
    """The store owns the file format; re-saves never leave a stale sibling behind."""

    def test_invalid_default_format_rejected(self, temp_dir):
        with pytest.raises(ValueError):
            CatalogFileStore(temp_dir, default_format="toml")

    def test_yaml_store_replaces_stale_json(self, temp_dir, sample_catalog):
        # An older run (json default) wrote catalog.json ...
        CatalogFileStore(temp_dir).save(sample_catalog)
        assert (temp_dir / "testdb" / "catalog.json").exists()

        # ... the store is now configured for yaml and re-analysis saves again.
        store = CatalogFileStore(temp_dir, default_format="yaml")
        newer = sample_catalog.model_copy(deep=True)
        newer.tables.pop("orders")
        store.save(newer)

        assert (temp_dir / "testdb" / "catalog.yaml").exists()
        assert not (temp_dir / "testdb" / "catalog.json").exists()
        loaded = store.load("testdb")
        assert loaded is not None
        assert list(loaded.tables) == ["users"]

    def test_internal_resaves_keep_default_format(self, temp_dir, sample_catalog):
        store = CatalogFileStore(temp_dir, default_format="yaml")
        review = CatalogReviewService(store)
        review.save_as_draft(sample_catalog)
        review.approve_table("testdb", "users")
        review.update_table_fields("testdb", "orders", {"tags": ["x"]})
        review.approve_catalog("testdb")

        files = sorted(p.name for p in (temp_dir / "testdb").iterdir())
        assert files == ["catalog.yaml"]
        loaded = store.load("testdb")
        assert loaded is not None and loaded.status.value == "approved"

    def test_explicit_format_overrides_default(self, temp_dir, sample_catalog):
        store = CatalogFileStore(temp_dir)  # json default
        store.save(sample_catalog, format="yaml")
        assert sorted(p.name for p in (temp_dir / "testdb").iterdir()) == ["catalog.yaml"]
        store.save(sample_catalog)  # back to the default -> json only
        assert sorted(p.name for p in (temp_dir / "testdb").iterdir()) == ["catalog.json"]

    def test_load_prefers_default_format_when_both_exist(self, temp_dir, sample_catalog):
        db_dir = temp_dir / "testdb"
        db_dir.mkdir()
        json_catalog = sample_catalog.model_copy(deep=True)
        json_catalog.tables.pop("orders")
        (db_dir / "catalog.json").write_text(json_catalog.model_dump_json())
        import yaml

        (db_dir / "catalog.yaml").write_text(yaml.safe_dump(sample_catalog.model_dump(mode="json")))

        yaml_first = CatalogFileStore(temp_dir, default_format="yaml").load("testdb")
        json_first = CatalogFileStore(temp_dir, default_format="json").load("testdb")
        assert yaml_first is not None and set(yaml_first.tables) == {"users", "orders"}
        assert json_first is not None and set(json_first.tables) == {"users"}
