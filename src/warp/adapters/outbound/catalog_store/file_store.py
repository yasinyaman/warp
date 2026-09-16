"""Catalog file storage.

Stores and loads DatabaseCatalog instances as JSON/YAML files (persistence only;
review transitions live in the domain, cross-catalog search in the application).
"""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import yaml

from warp.domain.catalog import (
    CatalogIndex,
    CatalogIndexEntry,
    DatabaseCatalog,
)
from warp.domain.catalog_naming import validate_catalog_name
from warp.domain.errors import (
    CatalogError,
    CatalogNotFoundError,
    InvalidCatalogNameError,
)

logger = logging.getLogger(__name__)


class CatalogFileStore:
    """File-based catalog storage.

    Stores each database catalog in its own directory:
        {base_path}/{db_name}/catalog.json

    An index file tracks all available catalogs:
        {base_path}/_index.json

    Usage:
        store = CatalogFileStore(Path("./catalogs"))
        store.save(catalog)
        loaded = store.load("my_database")
    """

    INDEX_FILE = "_index.json"
    FORMATS = ("json", "yaml")

    def __init__(self, base_path: str | Path, default_format: str = "json"):
        """Initialize CatalogFileStore.

        Args:
            base_path: Root directory for catalog storage
            default_format: File format used whenever a save does not name
                one explicitly ("json" or "yaml"). Every internal re-save
                (approve, edit, override merge) uses it, so a catalog never
                silently switches format.
        """
        if default_format not in self.FORMATS:
            raise ValueError(f"default_format must be one of {self.FORMATS}: {default_format!r}")
        self.base_path = Path(base_path)
        self.base_path.mkdir(parents=True, exist_ok=True)
        self.default_format = default_format

    def _db_dir(self, db_name: str) -> Path:
        """Resolve the directory for `db_name`, refusing anything outside the root.

        The name is validated first; the resolved path is then checked to be a
        direct child of the (resolved) base path as defense in depth against
        symlinks or platform path quirks.

        Raises:
            InvalidCatalogNameError: If the name is unsafe or escapes the root.
        """
        validate_catalog_name(db_name)
        db_dir = self.base_path / db_name
        if db_dir.resolve().parent != self.base_path.resolve():
            raise InvalidCatalogNameError(db_name)
        return db_dir

    def save(
        self,
        catalog: DatabaseCatalog,
        format: str | None = None,
    ) -> Path:
        """Save a database catalog to disk.

        Writes ``catalog.<format>`` and removes a stale sibling in the other
        format, so ``load()`` can never return an older copy.

        Args:
            catalog: DatabaseCatalog to save
            format: Output format - "json" or "yaml". Defaults to the store's
                ``default_format``.

        Returns:
            Path to the saved file

        Raises:
            CatalogError: If save fails
        """
        format = format or self.default_format
        if format not in self.FORMATS:
            raise CatalogError(f"Unsupported catalog format: {format!r}")

        db_dir = self._db_dir(catalog.database_name)
        db_dir.mkdir(parents=True, exist_ok=True)

        ext = format
        file_path = db_dir / f"catalog.{ext}"
        stale = [db_dir / f"catalog.{other}" for other in self.FORMATS if other != ext]

        try:
            data = catalog.model_dump(mode="json")

            if format == "yaml":
                with open(file_path, "w", encoding="utf-8") as f:
                    yaml.dump(
                        data,
                        f,
                        default_flow_style=False,
                        allow_unicode=True,
                        sort_keys=False,
                    )
            else:
                with open(file_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)

            for stale_path in stale:
                stale_path.unlink(missing_ok=True)

            self._update_index(catalog, str(file_path.relative_to(self.base_path)))

            logger.info(
                f"Catalog saved: {catalog.database_name} ({format}, {catalog.table_count} tables)"
            )

            return file_path

        except Exception as e:
            raise CatalogError(f"Failed to save catalog: {e}") from e

    def load(self, db_name: str) -> DatabaseCatalog | None:
        """Load a database catalog from disk.

        Tries the store's default format first, then the other one (a
        directory written by an older version may still hold both).

        Args:
            db_name: Database name

        Returns:
            DatabaseCatalog or None if not found
        """
        db_dir = self._db_dir(db_name)

        if not db_dir.exists():
            return None

        order = (self.default_format, *[f for f in self.FORMATS if f != self.default_format])
        for ext in order:
            file_path = db_dir / f"catalog.{ext}"
            if file_path.exists():
                try:
                    return self._load_file(file_path)
                except Exception as e:
                    logger.warning(f"Failed to load catalog file {file_path}: {e}")

        return None

    def load_or_raise(self, db_name: str) -> DatabaseCatalog:
        """Load a catalog or raise if not found.

        Args:
            db_name: Database name

        Returns:
            DatabaseCatalog

        Raises:
            CatalogNotFoundError: If catalog doesn't exist
        """
        catalog = self.load(db_name)
        if catalog is None:
            raise CatalogNotFoundError(db_name)
        return catalog

    def list_catalogs(self) -> list[str]:
        """List all available catalog names.

        Returns:
            List of database names with saved catalogs
        """
        catalogs = []
        for item in self.base_path.iterdir():
            if (
                item.is_dir()
                and not item.name.startswith("_")
                and ((item / "catalog.json").exists() or (item / "catalog.yaml").exists())
            ):
                catalogs.append(item.name)
        return sorted(catalogs)

    def delete(self, db_name: str) -> bool:
        """Delete a catalog and its directory.

        Args:
            db_name: Database name

        Returns:
            True if deleted, False if not found
        """
        db_dir = self._db_dir(db_name)
        if not db_dir.exists():
            return False

        import shutil

        shutil.rmtree(db_dir)

        index = self._load_index()
        if db_name in index.catalogs:
            del index.catalogs[db_name]
            self._save_index(index)

        logger.info(f"Catalog deleted: {db_name}")
        return True

    def get_index(self) -> CatalogIndex:
        """Get the catalog index."""
        return self._load_index()

    # --- Draft / Review methods ---

    # --- Cross-reference methods ---

    # --- Private helpers ---

    def _load_file(self, file_path: Path) -> DatabaseCatalog:
        """Load catalog from a specific file."""
        with open(file_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) if file_path.suffix == ".yaml" else json.load(f)

        return DatabaseCatalog(**data)

    def _load_index(self) -> CatalogIndex:
        """Load or create the catalog index."""
        index_path = self.base_path / self.INDEX_FILE
        if index_path.exists():
            try:
                with open(index_path, encoding="utf-8") as f:
                    data = json.load(f)
                return CatalogIndex(**data)
            except Exception:
                pass
        return CatalogIndex()

    def _save_index(self, index: CatalogIndex) -> None:
        """Save the catalog index."""
        index_path = self.base_path / self.INDEX_FILE
        index.updated_at = datetime.now(UTC)
        with open(index_path, "w", encoding="utf-8") as f:
            json.dump(index.model_dump(mode="json"), f, indent=2, ensure_ascii=False)

    def _update_index(self, catalog: DatabaseCatalog, relative_path: str) -> None:
        """Update index with catalog metadata."""
        index = self._load_index()
        index.catalogs[catalog.database_name] = CatalogIndexEntry(
            database_name=catalog.database_name,
            database_type=catalog.database_type,
            table_count=catalog.table_count,
            languages=catalog.languages,
            generated_at=catalog.generated_at,
            updated_at=catalog.updated_at,
            file_path=relative_path,
            version=catalog.version,
            status=catalog.status.value,
        )
        self._save_index(index)
