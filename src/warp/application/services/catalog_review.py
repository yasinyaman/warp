"""Catalog review workflow: draft -> edit/approve -> approved, on top of a repository."""

from pathlib import Path
from typing import Any

from warp.application.ports.catalog_repository import CatalogRepository
from warp.domain import catalog_review as rules
from warp.domain.catalog import CatalogIndex, ColumnCatalogEntry, DatabaseCatalog, TableCatalogEntry


class CatalogReviewService:
    """Load -> apply a pure transition -> save."""

    def __init__(self, repository: CatalogRepository):
        """Wrap `repository`."""
        self.repository = repository

    # -- persistence pass-throughs -------------------------------------------------
    def load(self, db_name: str) -> DatabaseCatalog | None:
        """Load a catalog, or None."""
        return self.repository.load(db_name)

    def load_or_raise(self, db_name: str) -> DatabaseCatalog:
        """Load a catalog or raise `CatalogNotFoundError`."""
        return self.repository.load_or_raise(db_name)

    def list_catalogs(self) -> list[str]:
        """Names of stored catalogs."""
        return self.repository.list_catalogs()

    def get_index(self) -> CatalogIndex:
        """Summary index of stored catalogs."""
        return self.repository.get_index()

    def delete(self, db_name: str) -> bool:
        """Delete a catalog."""
        return self.repository.delete(db_name)

    # -- transitions ------------------------------------------------------------------
    def save_as_draft(self, catalog: DatabaseCatalog, format: str | None = None) -> Path:
        """Reset to draft (all tables pending) and persist."""
        return self.repository.save(rules.mark_as_draft(catalog), format=format)

    def approve_table(self, db_name: str, table_name: str) -> DatabaseCatalog:
        """Approve one table and persist."""
        catalog = rules.approve_table(self.repository.load_or_raise(db_name), table_name)
        self.repository.save(catalog)
        return catalog

    def approve_catalog(self, db_name: str) -> DatabaseCatalog:
        """Approve every pending table, finalize and persist."""
        catalog = rules.approve_catalog(self.repository.load_or_raise(db_name))
        self.repository.save(catalog)
        return catalog

    def update_table_fields(
        self, db_name: str, table_name: str, updates: dict[str, Any], lang: str = "en"
    ) -> TableCatalogEntry:
        """Apply table edits and persist."""
        catalog = self.repository.load_or_raise(db_name)
        table = rules.update_table_fields(catalog, table_name, updates, lang)
        self.repository.save(catalog)
        return table

    def update_column_fields(
        self,
        db_name: str,
        table_name: str,
        column_name: str,
        updates: dict[str, Any],
        lang: str = "en",
    ) -> ColumnCatalogEntry:
        """Apply column edits and persist."""
        catalog = self.repository.load_or_raise(db_name)
        column = rules.update_column_fields(catalog, table_name, column_name, updates, lang)
        self.repository.save(catalog)
        return column

    def extract_overrides(self, db_name: str) -> dict[str, dict[str, Any]]:
        """User edits of an existing catalog (empty when none is stored)."""
        catalog = self.repository.load(db_name)
        return rules.extract_overrides(catalog) if catalog else {}

    def apply_overrides(
        self, db_name: str, overrides: dict[str, dict[str, Any]]
    ) -> DatabaseCatalog:
        """Re-apply saved user edits and persist."""
        catalog = rules.apply_overrides(self.repository.load_or_raise(db_name), overrides)
        self.repository.save(catalog)
        return catalog
