"""Catalog persistence port."""

from pathlib import Path
from typing import Protocol

from warp.domain.catalog import CatalogIndex, DatabaseCatalog


class CatalogRepository(Protocol):
    """Stores and retrieves whole catalogs by database name."""

    def save(self, catalog: DatabaseCatalog, format: str | None = None) -> Path:
        """Persist a catalog; returns where it was written."""
        ...

    def load(self, db_name: str) -> DatabaseCatalog | None:
        """Load a catalog, or None when it does not exist."""
        ...

    def load_or_raise(self, db_name: str) -> DatabaseCatalog:
        """Load a catalog or raise `CatalogNotFoundError`."""
        ...

    def list_catalogs(self) -> list[str]:
        """Names of every stored catalog."""
        ...

    def delete(self, db_name: str) -> bool:
        """Remove a catalog; True when it existed."""
        ...

    def get_index(self) -> CatalogIndex:
        """Summary index of stored catalogs."""
        ...
