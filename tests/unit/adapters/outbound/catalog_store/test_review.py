"""Tests for HITL (Human-in-the-Loop) review system.

Tests cover:
- CatalogStatus and TableReviewStatus enums
- Draft save/load
- Table and catalog approval
- Table and column field editing
- Backward compatibility with old catalogs
"""

import json
import tempfile
from pathlib import Path

import pytest

from warp.adapters.outbound.catalog_store.file_store import CatalogFileStore
from warp.application.services.catalog_review import CatalogReviewService
from warp.domain.catalog import (
    CatalogStatus,
    ColumnCatalogEntry,
    DatabaseCatalog,
    LocalizedText,
    RelationshipInfo,
    TableCatalogEntry,
    TableReviewStatus,
)
from warp.domain.errors import (
    CatalogNotFoundError,
    ColumnNotFoundInCatalogError,
    TableNotFoundInCatalogError,
)


class ReviewStore(CatalogReviewService):
    """Review service over a real file store that also exposes `save` for seeding."""

    def __init__(self, base_path, default_format="json"):
        self.file_store = CatalogFileStore(base_path, default_format=default_format)
        super().__init__(self.file_store)

    def save(self, catalog, format=None):
        return self.file_store.save(catalog, format=format)

    @property
    def base_path(self):
        return self.file_store.base_path


@pytest.fixture
def temp_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def store(temp_dir):
    return ReviewStore(temp_dir)


def _make_catalog(db_name: str = "testdb") -> DatabaseCatalog:
    """Create a sample catalog for testing (not a fixture)."""
    return DatabaseCatalog(
        database_name=db_name,
        database_type="postgresql",
        status=CatalogStatus.draft,
        tables={
            "users": TableCatalogEntry(
                table_name="users",
                description=LocalizedText(texts={"en": "User accounts"}),
                human_name=LocalizedText(texts={"en": "Users"}),
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
                        description=LocalizedText(texts={"en": "User email address"}),
                        tags=["contact", "pii"],
                    ),
                    ColumnCatalogEntry(
                        name="name",
                        data_type="varchar",
                        description=LocalizedText(texts={"en": "Full name"}),
                    ),
                ],
                tags=["auth", "core"],
                review_status=TableReviewStatus.pending,
            ),
            "orders": TableCatalogEntry(
                table_name="orders",
                description=LocalizedText(texts={"en": "Customer orders"}),
                human_name=LocalizedText(texts={"en": "Orders"}),
                columns=[
                    ColumnCatalogEntry(
                        name="id",
                        data_type="integer",
                        is_primary_key=True,
                    ),
                    ColumnCatalogEntry(
                        name="user_id",
                        data_type="integer",
                        is_foreign_key=True,
                        references="users.id",
                    ),
                    ColumnCatalogEntry(
                        name="total",
                        data_type="decimal",
                        semantic_type="amount",
                        description=LocalizedText(texts={"en": "Order total"}),
                        tags=["financial"],
                    ),
                ],
                tags=["commerce"],
                review_status=TableReviewStatus.pending,
            ),
        },
        languages=["en"],
    )


@pytest.fixture
def draft_catalog():
    """Create a sample draft catalog with two tables."""
    return DatabaseCatalog(
        database_name="testdb",
        database_type="postgresql",
        status=CatalogStatus.draft,
        tables={
            "users": TableCatalogEntry(
                table_name="users",
                description=LocalizedText(texts={"en": "User accounts"}),
                human_name=LocalizedText(texts={"en": "Users"}),
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
                        description=LocalizedText(texts={"en": "User email address"}),
                        tags=["contact", "pii"],
                    ),
                    ColumnCatalogEntry(
                        name="name",
                        data_type="varchar",
                        description=LocalizedText(texts={"en": "Full name"}),
                    ),
                ],
                tags=["auth", "core"],
                relationships=[
                    RelationshipInfo(
                        source_column="id",
                        target_table="orders",
                        target_column="user_id",
                        relationship_type="one-to-many",
                        description=LocalizedText(texts={"en": "User has many orders"}),
                    ),
                ],
                review_status=TableReviewStatus.pending,
            ),
            "orders": TableCatalogEntry(
                table_name="orders",
                description=LocalizedText(texts={"en": "Customer orders"}),
                human_name=LocalizedText(texts={"en": "Orders"}),
                columns=[
                    ColumnCatalogEntry(
                        name="id",
                        data_type="integer",
                        is_primary_key=True,
                    ),
                    ColumnCatalogEntry(
                        name="user_id",
                        data_type="integer",
                        is_foreign_key=True,
                        references="users.id",
                    ),
                    ColumnCatalogEntry(
                        name="total",
                        data_type="decimal",
                        semantic_type="amount",
                        description=LocalizedText(texts={"en": "Order total"}),
                        tags=["financial"],
                    ),
                ],
                tags=["commerce"],
                review_status=TableReviewStatus.pending,
            ),
        },
        languages=["en"],
    )


# ---- Status Enum and Model Tests ----


class TestCatalogStatus:
    """Tests for status enums and model fields."""

    def test_default_catalog_status_is_draft(self):
        catalog = DatabaseCatalog(database_name="test")
        assert catalog.status == CatalogStatus.draft

    def test_default_table_review_status_is_pending(self):
        table = TableCatalogEntry(table_name="test")
        assert table.review_status == TableReviewStatus.pending

    def test_catalog_status_values(self):
        assert CatalogStatus.draft.value == "draft"
        assert CatalogStatus.approved.value == "approved"

    def test_table_review_status_values(self):
        assert TableReviewStatus.pending.value == "pending"
        assert TableReviewStatus.approved.value == "approved"
        assert TableReviewStatus.modified.value == "modified"

    def test_all_tables_approved_false(self, draft_catalog):
        assert not draft_catalog.all_tables_approved

    def test_all_tables_approved_true(self, draft_catalog):
        for t in draft_catalog.tables.values():
            t.review_status = TableReviewStatus.approved
        assert draft_catalog.all_tables_approved

    def test_all_tables_approved_mixed(self, draft_catalog):
        draft_catalog.tables["users"].review_status = TableReviewStatus.approved
        draft_catalog.tables["orders"].review_status = TableReviewStatus.modified
        assert draft_catalog.all_tables_approved

    def test_review_summary_all_pending(self, draft_catalog):
        summary = draft_catalog.review_summary
        assert summary == {"pending": 2}

    def test_review_summary_mixed(self, draft_catalog):
        draft_catalog.tables["users"].review_status = TableReviewStatus.approved
        summary = draft_catalog.review_summary
        assert summary == {"pending": 1, "approved": 1}

    def test_serialization_roundtrip(self, draft_catalog):
        """Ensure status survives JSON serialization/deserialization."""
        data = json.loads(draft_catalog.model_dump_json())
        restored = DatabaseCatalog(**data)
        assert restored.status == CatalogStatus.draft
        assert restored.tables["users"].review_status == TableReviewStatus.pending
        assert restored.tables["orders"].review_status == TableReviewStatus.pending


# ---- Draft Save/Load Tests ----


class TestSaveAsDraft:
    def test_save_as_draft(self, store, draft_catalog):
        path = store.save_as_draft(draft_catalog)
        assert path.exists()

        loaded = store.load("testdb")
        assert loaded is not None
        assert loaded.status == CatalogStatus.draft
        for t in loaded.tables.values():
            assert t.review_status == TableReviewStatus.pending

    def test_save_as_draft_updates_index(self, store, draft_catalog):
        store.save_as_draft(draft_catalog)
        index = store.get_index()
        entry = index.catalogs.get("testdb")
        assert entry is not None
        assert entry.status == "draft"


# ---- Table Approval Tests ----


class TestApproveTable:
    def test_approve_single_table(self, store, draft_catalog):
        store.save(draft_catalog)
        catalog = store.approve_table("testdb", "users")
        assert catalog.tables["users"].review_status == TableReviewStatus.approved
        assert catalog.tables["orders"].review_status == TableReviewStatus.pending
        assert catalog.status == CatalogStatus.draft  # Still draft

    def test_approve_nonexistent_table_raises(self, store, draft_catalog):
        store.save(draft_catalog)
        with pytest.raises(TableNotFoundInCatalogError):
            store.approve_table("testdb", "nonexistent")

    def test_approve_nonexistent_catalog_raises(self, store):
        with pytest.raises(CatalogNotFoundError):
            store.approve_table("nonexistent", "users")

    def test_approve_persists_to_disk(self, store, draft_catalog):
        store.save(draft_catalog)
        store.approve_table("testdb", "users")

        # Reload from disk
        loaded = store.load("testdb")
        assert loaded.tables["users"].review_status == TableReviewStatus.approved


# ---- Catalog Approval Tests ----


class TestApproveCatalog:
    def test_approve_all(self, store, draft_catalog):
        store.save(draft_catalog)
        catalog = store.approve_catalog("testdb")
        assert catalog.status == CatalogStatus.approved
        for t in catalog.tables.values():
            assert t.review_status == TableReviewStatus.approved

    def test_approve_keeps_modified_status(self, store, draft_catalog):
        """Modified tables should stay modified, not become approved."""
        store.save(draft_catalog)
        # First modify a table
        store.update_table_fields("testdb", "users", {"tags": ["updated"]})
        # Then approve catalog
        catalog = store.approve_catalog("testdb")
        assert catalog.status == CatalogStatus.approved
        # Modified table stays modified, pending becomes approved
        assert catalog.tables["users"].review_status == TableReviewStatus.modified
        assert catalog.tables["orders"].review_status == TableReviewStatus.approved

    def test_approve_updates_index(self, store, draft_catalog):
        store.save(draft_catalog)
        store.approve_catalog("testdb")
        index = store.get_index()
        assert index.catalogs["testdb"].status == "approved"


# ---- Table Field Update Tests ----


class TestUpdateTableFields:
    def test_update_description_string(self, store, draft_catalog):
        store.save(draft_catalog)
        table = store.update_table_fields(
            "testdb",
            "users",
            {"description": "Updated user accounts table"},
            lang="en",
        )
        assert table.description.get("en") == "Updated user accounts table"

    def test_update_description_dict(self, store, draft_catalog):
        store.save(draft_catalog)
        table = store.update_table_fields(
            "testdb",
            "users",
            {"description": {"en": "User Accounts", "tr": "Kullanici Hesaplari"}},
        )
        assert table.description.get("en") == "User Accounts"
        assert table.description.get("tr") == "Kullanici Hesaplari"

    def test_update_human_name(self, store, draft_catalog):
        store.save(draft_catalog)
        table = store.update_table_fields(
            "testdb",
            "users",
            {"human_name": "User Accounts"},
            lang="en",
        )
        assert table.human_name.get("en") == "User Accounts"

    def test_update_tags(self, store, draft_catalog):
        store.save(draft_catalog)
        table = store.update_table_fields(
            "testdb",
            "users",
            {"tags": ["new_tag", "core"]},
        )
        assert table.tags == ["new_tag", "core"]

    def test_update_marks_as_modified(self, store, draft_catalog):
        store.save(draft_catalog)
        table = store.update_table_fields(
            "testdb",
            "users",
            {"description": "Changed"},
            lang="en",
        )
        assert table.review_status == TableReviewStatus.modified

    def test_update_nonexistent_table_raises(self, store, draft_catalog):
        store.save(draft_catalog)
        with pytest.raises(TableNotFoundInCatalogError):
            store.update_table_fields("testdb", "nonexistent", {"tags": []})

    def test_update_persists_to_disk(self, store, draft_catalog):
        store.save(draft_catalog)
        store.update_table_fields(
            "testdb",
            "users",
            {"description": "Persisted description"},
            lang="en",
        )
        loaded = store.load("testdb")
        assert loaded.tables["users"].description.get("en") == "Persisted description"

    def test_update_multiple_fields_at_once(self, store, draft_catalog):
        store.save(draft_catalog)
        table = store.update_table_fields(
            "testdb",
            "users",
            {
                "description": "New desc",
                "human_name": "New Name",
                "tags": ["tag1", "tag2"],
            },
            lang="en",
        )
        assert table.description.get("en") == "New desc"
        assert table.human_name.get("en") == "New Name"
        assert table.tags == ["tag1", "tag2"]


# ---- Column Field Update Tests ----


class TestUpdateColumnFields:
    def test_update_column_description(self, store, draft_catalog):
        store.save(draft_catalog)
        col = store.update_column_fields(
            "testdb",
            "users",
            "email",
            {"description": "Primary email address"},
            lang="en",
        )
        assert col.description.get("en") == "Primary email address"

    def test_update_semantic_type(self, store, draft_catalog):
        store.save(draft_catalog)
        col = store.update_column_fields(
            "testdb",
            "users",
            "email",
            {"semantic_type": "primary_email"},
        )
        assert col.semantic_type == "primary_email"

    def test_update_column_tags(self, store, draft_catalog):
        store.save(draft_catalog)
        col = store.update_column_fields(
            "testdb",
            "users",
            "email",
            {"tags": ["pii", "unique"]},
        )
        assert col.tags == ["pii", "unique"]

    def test_update_marks_parent_table_modified(self, store, draft_catalog):
        store.save(draft_catalog)
        store.update_column_fields(
            "testdb",
            "users",
            "email",
            {"tags": ["updated"]},
        )
        catalog = store.load("testdb")
        assert catalog.tables["users"].review_status == TableReviewStatus.modified

    def test_update_nonexistent_column_raises(self, store, draft_catalog):
        store.save(draft_catalog)
        with pytest.raises(ColumnNotFoundInCatalogError):
            store.update_column_fields(
                "testdb",
                "users",
                "nonexistent",
                {"description": "test"},
            )

    def test_update_nonexistent_table_raises(self, store, draft_catalog):
        store.save(draft_catalog)
        with pytest.raises(TableNotFoundInCatalogError):
            store.update_column_fields(
                "testdb",
                "nonexistent",
                "id",
                {"description": "test"},
            )

    def test_update_column_description_dict(self, store, draft_catalog):
        store.save(draft_catalog)
        col = store.update_column_fields(
            "testdb",
            "users",
            "email",
            {"description": {"en": "Email", "tr": "E-posta"}},
        )
        assert col.description.get("en") == "Email"
        assert col.description.get("tr") == "E-posta"

    def test_clear_semantic_type(self, store, draft_catalog):
        store.save(draft_catalog)
        col = store.update_column_fields(
            "testdb",
            "users",
            "email",
            {"semantic_type": None},
        )
        assert col.semantic_type is None


# ---- List Drafts Tests ----


# ---- Backward Compatibility Tests ----


class TestBackwardCompatibility:
    """Ensure old catalogs without status fields load correctly."""

    def test_load_old_catalog_gets_default_status(self, store):
        """Old catalog JSON without status fields should get defaults."""
        db_dir = store.base_path / "olddb"
        db_dir.mkdir()
        old_data = {
            "database_name": "olddb",
            "database_type": "postgresql",
            "tables": {
                "t1": {
                    "table_name": "t1",
                    "columns": [],
                }
            },
            "languages": ["en"],
            "generated_at": "2025-01-01T00:00:00Z",
        }
        with open(db_dir / "catalog.json", "w") as f:
            json.dump(old_data, f)

        catalog = store.load("olddb")
        assert catalog is not None
        assert catalog.status == CatalogStatus.draft
        assert catalog.tables["t1"].review_status == TableReviewStatus.pending

    def test_old_index_without_status(self, store):
        """Old index without status field should load with defaults."""
        index_data = {
            "catalogs": {
                "old": {
                    "database_name": "old",
                    "table_count": 3,
                }
            },
            "created_at": "2025-01-01T00:00:00Z",
            "updated_at": "2025-01-01T00:00:00Z",
        }
        with open(store.base_path / "_index.json", "w") as f:
            json.dump(index_data, f)

        index = store.get_index()
        assert index.catalogs["old"].status == "draft"


# ---- Exception Tests ----


class TestReviewExceptions:
    def test_column_not_found_error(self):
        from warp.domain.errors import ColumnNotFoundInCatalogError

        err = ColumnNotFoundInCatalogError("email", "users", "mydb")
        assert "email" in str(err)
        assert "users" in str(err)

    def test_catalog_not_draft_error(self):
        from warp.domain.errors import CatalogNotDraftError

        err = CatalogNotDraftError("mydb")
        assert "draft" in str(err).lower()
        assert err.status_code == 409


# ---- User Overrides Tracking Tests ----


class TestUserOverridesTracking:
    """Tests that user_overrides are populated when editing table/column fields."""

    @pytest.fixture
    def store_with_draft(self, tmp_path):
        store = ReviewStore(tmp_path)
        catalog = _make_catalog("overdb")
        store.save_as_draft(catalog)
        return store

    def test_update_table_description_tracks_override(self, store_with_draft):
        """Editing table description should populate user_overrides."""
        store = store_with_draft
        table = store.update_table_fields(
            "overdb", "users", {"description": "Custom desc"}, lang="en"
        )
        assert table.user_overrides["description"] == {"en": "Custom desc"}

    def test_update_table_description_dict_tracks_override(self, store_with_draft):
        """Editing table description with dict should populate user_overrides."""
        store = store_with_draft
        table = store.update_table_fields(
            "overdb",
            "users",
            {"description": {"en": "English desc", "tr": "Turkce aciklama"}},
        )
        assert table.user_overrides["description"]["en"] == "English desc"
        assert table.user_overrides["description"]["tr"] == "Turkce aciklama"

    def test_update_table_human_name_tracks_override(self, store_with_draft):
        store = store_with_draft
        table = store.update_table_fields(
            "overdb", "users", {"human_name": "Custom Users"}, lang="en"
        )
        assert table.user_overrides["human_name"] == {"en": "Custom Users"}

    def test_update_table_tags_tracks_override(self, store_with_draft):
        store = store_with_draft
        table = store.update_table_fields("overdb", "users", {"tags": ["core", "auth"]})
        assert table.user_overrides["tags"] == ["core", "auth"]

    def test_update_table_relationships_tracks_override(self, store_with_draft):
        store = store_with_draft
        rels = [{"source_column": "id", "target_table": "orders", "target_column": "user_id"}]
        table = store.update_table_fields("overdb", "users", {"relationships": rels})
        assert table.user_overrides["relationships"] == rels

    def test_update_column_description_tracks_override(self, store_with_draft):
        store = store_with_draft
        col = store.update_column_fields(
            "overdb", "users", "id", {"description": "Primary key"}, lang="en"
        )
        assert col.user_overrides["description"] == {"en": "Primary key"}

    def test_update_column_semantic_type_tracks_override(self, store_with_draft):
        store = store_with_draft
        col = store.update_column_fields(
            "overdb", "users", "email", {"semantic_type": "email_address"}
        )
        assert col.user_overrides["semantic_type"] == "email_address"

    def test_update_column_tags_tracks_override(self, store_with_draft):
        store = store_with_draft
        col = store.update_column_fields("overdb", "users", "email", {"tags": ["pii", "unique"]})
        assert col.user_overrides["tags"] == ["pii", "unique"]

    def test_multiple_edits_accumulate_overrides(self, store_with_draft):
        """Multiple edits should accumulate in user_overrides."""
        store = store_with_draft
        store.update_table_fields("overdb", "users", {"description": "Desc 1"}, lang="en")
        store.update_table_fields("overdb", "users", {"human_name": "My Users"}, lang="en")
        store.update_table_fields("overdb", "users", {"tags": ["important"]})

        catalog = store.load("overdb")
        table = catalog.get_table("users")
        assert "description" in table.user_overrides
        assert "human_name" in table.user_overrides
        assert "tags" in table.user_overrides

    def test_multiple_column_edits_accumulate(self, store_with_draft):
        """Multiple column edits should accumulate in user_overrides."""
        store = store_with_draft
        store.update_column_fields(
            "overdb", "users", "email", {"description": "User email"}, lang="en"
        )
        store.update_column_fields("overdb", "users", "email", {"semantic_type": "email"})
        store.update_column_fields("overdb", "users", "email", {"tags": ["pii"]})

        catalog = store.load("overdb")
        col = catalog.get_table("users").get_column("email")
        assert col.user_overrides["description"] == {"en": "User email"}
        assert col.user_overrides["semantic_type"] == "email"
        assert col.user_overrides["tags"] == ["pii"]


# ---- Extract Overrides Tests ----


class TestExtractOverrides:
    """Tests for extracting user_overrides from an existing catalog."""

    @pytest.fixture
    def store_with_edited_catalog(self, tmp_path):
        store = ReviewStore(tmp_path)
        catalog = _make_catalog("extractdb")
        store.save_as_draft(catalog)
        # Make some edits
        store.update_table_fields("extractdb", "users", {"description": "Custom users"}, lang="en")
        store.update_table_fields("extractdb", "users", {"tags": ["core"]})
        store.update_column_fields("extractdb", "users", "email", {"semantic_type": "email"})
        store.update_column_fields(
            "extractdb", "users", "email", {"description": "User email"}, lang="en"
        )
        return store

    def test_extract_overrides_returns_table_overrides(self, store_with_edited_catalog):
        overrides = store_with_edited_catalog.extract_overrides("extractdb")
        assert "users" in overrides
        assert overrides["users"]["description"] == {"en": "Custom users"}
        assert overrides["users"]["tags"] == ["core"]

    def test_extract_overrides_returns_column_overrides(self, store_with_edited_catalog):
        overrides = store_with_edited_catalog.extract_overrides("extractdb")
        assert "columns" in overrides["users"]
        assert "email" in overrides["users"]["columns"]
        assert overrides["users"]["columns"]["email"]["semantic_type"] == "email"
        assert overrides["users"]["columns"]["email"]["description"] == {"en": "User email"}

    def test_extract_overrides_skips_unedited_tables(self, store_with_edited_catalog):
        overrides = store_with_edited_catalog.extract_overrides("extractdb")
        assert "orders" not in overrides

    def test_extract_overrides_nonexistent_catalog(self, tmp_path):
        store = ReviewStore(tmp_path)
        overrides = store.extract_overrides("nonexistent")
        assert overrides == {}

    def test_extract_overrides_no_edits(self, tmp_path):
        store = ReviewStore(tmp_path)
        catalog = _make_catalog("cleandb")
        store.save_as_draft(catalog)
        overrides = store.extract_overrides("cleandb")
        assert overrides == {}


# ---- Apply Overrides Tests ----


class TestApplyOverrides:
    """Tests for applying saved overrides to a fresh catalog."""

    @pytest.fixture
    def store(self, tmp_path):
        return ReviewStore(tmp_path)

    def test_apply_table_description_override(self, store):
        catalog = _make_catalog("applydb")
        store.save_as_draft(catalog)
        overrides = {
            "users": {
                "description": {"en": "Custom users table"},
            }
        }
        result = store.apply_overrides("applydb", overrides)
        assert result.get_table("users").description.get("en") == "Custom users table"

    def test_apply_table_human_name_override(self, store):
        catalog = _make_catalog("applydb")
        store.save_as_draft(catalog)
        overrides = {
            "users": {
                "human_name": {"en": "Application Users", "tr": "Uygulama Kullanicilari"},
            }
        }
        result = store.apply_overrides("applydb", overrides)
        table = result.get_table("users")
        assert table.human_name.get("en") == "Application Users"
        assert table.human_name.get("tr") == "Uygulama Kullanicilari"

    def test_apply_table_tags_override(self, store):
        catalog = _make_catalog("applydb")
        store.save_as_draft(catalog)
        overrides = {"users": {"tags": ["core", "auth"]}}
        result = store.apply_overrides("applydb", overrides)
        assert result.get_table("users").tags == ["core", "auth"]

    def test_apply_column_overrides(self, store):
        catalog = _make_catalog("applydb")
        store.save_as_draft(catalog)
        overrides = {
            "users": {
                "columns": {
                    "email": {
                        "description": {"en": "Primary email address"},
                        "semantic_type": "email",
                        "tags": ["pii", "unique"],
                    }
                }
            }
        }
        result = store.apply_overrides("applydb", overrides)
        col = result.get_table("users").get_column("email")
        assert col.description.get("en") == "Primary email address"
        assert col.semantic_type == "email"
        assert col.tags == ["pii", "unique"]
        assert col.user_overrides["semantic_type"] == "email"

    def test_apply_overrides_marks_table_modified(self, store):
        catalog = _make_catalog("applydb")
        store.save_as_draft(catalog)
        overrides = {"users": {"description": {"en": "Modified"}}}
        result = store.apply_overrides("applydb", overrides)
        assert result.get_table("users").review_status == TableReviewStatus.modified

    def test_apply_overrides_preserves_user_overrides_dict(self, store):
        """After applying, user_overrides dict should be restored."""
        catalog = _make_catalog("applydb")
        store.save_as_draft(catalog)
        overrides = {
            "users": {
                "description": {"en": "Custom"},
                "tags": ["important"],
            }
        }
        result = store.apply_overrides("applydb", overrides)
        table = result.get_table("users")
        assert table.user_overrides["description"] == {"en": "Custom"}
        assert table.user_overrides["tags"] == ["important"]

    def test_apply_overrides_skips_missing_table(self, store):
        """Overrides for tables that don't exist should be silently skipped."""
        catalog = _make_catalog("applydb")
        store.save_as_draft(catalog)
        overrides = {
            "nonexistent_table": {"description": {"en": "Wont apply"}},
            "users": {"description": {"en": "Will apply"}},
        }
        result = store.apply_overrides("applydb", overrides)
        assert result.get_table("users").description.get("en") == "Will apply"
        assert result.get_table("nonexistent_table") is None

    def test_apply_overrides_skips_missing_column(self, store):
        """Overrides for columns that don't exist should be silently skipped."""
        catalog = _make_catalog("applydb")
        store.save_as_draft(catalog)
        overrides = {
            "users": {
                "columns": {
                    "nonexistent_col": {"semantic_type": "foo"},
                    "email": {"semantic_type": "email"},
                }
            }
        }
        result = store.apply_overrides("applydb", overrides)
        col = result.get_table("users").get_column("email")
        assert col.semantic_type == "email"


# ---- Full Override Cycle Tests ----


class TestOverrideFullCycle:
    """End-to-end tests: edit -> extract -> regenerate -> apply -> verify."""

    def test_full_override_cycle(self, tmp_path):
        """Full cycle: draft -> edit -> extract -> new draft -> apply -> verify."""
        store = ReviewStore(tmp_path)

        # Step 1: Create initial draft
        catalog1 = _make_catalog("cycledb")
        store.save_as_draft(catalog1)

        # Step 2: User edits
        store.update_table_fields(
            "cycledb",
            "users",
            {
                "description": "Custom user table description",
                "human_name": "Application Users",
                "tags": ["core", "auth"],
            },
            lang="en",
        )
        store.update_column_fields(
            "cycledb",
            "users",
            "email",
            {
                "description": "Primary email",
                "semantic_type": "email",
                "tags": ["pii"],
            },
            lang="en",
        )

        # Step 3: Extract overrides
        overrides = store.extract_overrides("cycledb")
        assert "users" in overrides
        assert "columns" in overrides["users"]
        assert "email" in overrides["users"]["columns"]

        # Step 4: Simulate regeneration (new LLM-generated catalog)
        catalog2 = _make_catalog("cycledb")
        # The new catalog has fresh LLM descriptions, no user edits
        assert catalog2.get_table("users").user_overrides == {}
        store.save_as_draft(catalog2)

        # Step 5: Re-apply overrides
        result = store.apply_overrides("cycledb", overrides)

        # Step 6: Verify all user edits survived
        table = result.get_table("users")
        assert table.description.get("en") == "Custom user table description"
        assert table.human_name.get("en") == "Application Users"
        assert table.tags == ["core", "auth"]
        assert table.review_status == TableReviewStatus.modified

        col = table.get_column("email")
        assert col.description.get("en") == "Primary email"
        assert col.semantic_type == "email"
        assert col.tags == ["pii"]

        # Verify user_overrides are preserved for next regeneration
        assert table.user_overrides["description"] == {"en": "Custom user table description"}
        assert col.user_overrides["semantic_type"] == "email"

    def test_cycle_with_schema_change(self, tmp_path):
        """Override cycle where schema changes (table/column removed)."""
        store = ReviewStore(tmp_path)

        # Create catalog with 2 tables
        catalog1 = _make_catalog("schemadb")
        store.save_as_draft(catalog1)

        # Edit both tables
        store.update_table_fields("schemadb", "users", {"description": "Custom users"}, lang="en")
        store.update_table_fields("schemadb", "orders", {"description": "Custom orders"}, lang="en")

        # Extract
        overrides = store.extract_overrides("schemadb")
        assert "users" in overrides
        assert "orders" in overrides

        # Regenerate with only 'users' table (orders removed from schema)
        catalog2 = DatabaseCatalog(
            database_name="schemadb",
            database_type="postgresql",
            tables={
                "users": TableCatalogEntry(
                    table_name="users",
                    columns=[
                        ColumnCatalogEntry(name="id", data_type="integer"),
                        ColumnCatalogEntry(name="email", data_type="varchar"),
                    ],
                ),
            },
        )
        store.save_as_draft(catalog2)

        # Apply overrides — orders override should be silently skipped
        result = store.apply_overrides("schemadb", overrides)
        assert result.get_table("users").description.get("en") == "Custom users"
        assert result.get_table("orders") is None

    def test_cycle_preserves_on_reload(self, tmp_path):
        """Overrides should persist through save/load cycles."""
        store = ReviewStore(tmp_path)

        catalog = _make_catalog("persistdb")
        store.save_as_draft(catalog)
        store.update_table_fields(
            "persistdb", "users", {"description": "Persisted desc"}, lang="en"
        )
        store.update_column_fields("persistdb", "users", "email", {"semantic_type": "email"})

        # Reload from disk
        loaded = store.load("persistdb")
        table = loaded.get_table("users")
        assert table.user_overrides["description"] == {"en": "Persisted desc"}
        col = table.get_column("email")
        assert col.user_overrides["semantic_type"] == "email"


# ---- Backward Compatibility with Overrides ----


class TestOverrideBackwardCompatibility:
    """Test that catalogs without user_overrides load correctly."""

    def test_old_catalog_has_empty_overrides(self, tmp_path):
        """Catalogs saved before user_overrides feature have empty dicts."""
        store = ReviewStore(tmp_path)
        db_dir = tmp_path / "oldoverdb"
        db_dir.mkdir()
        old_data = {
            "database_name": "oldoverdb",
            "database_type": "postgresql",
            "tables": {
                "t1": {
                    "table_name": "t1",
                    "description": {"texts": {"en": "Old table"}},
                    "human_name": {"texts": {"en": "T1"}},
                    "columns": [
                        {"name": "id", "data_type": "integer"},
                        {"name": "name", "data_type": "varchar"},
                    ],
                }
            },
            "generated_at": "2025-01-01T00:00:00Z",
        }
        with open(db_dir / "catalog.json", "w") as f:
            json.dump(old_data, f)

        catalog = store.load("oldoverdb")
        assert catalog is not None
        table = catalog.get_table("t1")
        assert table.user_overrides == {}
        for col in table.columns:
            assert col.user_overrides == {}

    def test_extract_overrides_from_old_catalog(self, tmp_path):
        """Extracting overrides from old catalog (no overrides) returns empty."""
        store = ReviewStore(tmp_path)
        catalog = _make_catalog("olddb2")
        store.save(catalog)  # Save without overrides
        overrides = store.extract_overrides("olddb2")
        assert overrides == {}
