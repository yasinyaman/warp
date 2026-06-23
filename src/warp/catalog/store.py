"""Catalog file storage.

Stores and loads DatabaseCatalog instances as JSON/YAML files.
Provides cross-catalog search capabilities.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from warp.catalog.models import (
    CatalogIndex,
    CatalogIndexEntry,
    CatalogStatus,
    ColumnCatalogEntry,
    DatabaseCatalog,
    RelationshipInfo,
    TableCatalogEntry,
    TableReviewStatus,
)
from warp.core.exceptions import (
    CatalogError,
    CatalogNotFoundError,
    ColumnNotFoundInCatalogError,
    TableNotFoundInCatalogError,
)
from warp.core.logging import get_logger

logger = get_logger(__name__)


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

    def __init__(self, base_path: str | Path):
        """Initialize CatalogFileStore.

        Args:
            base_path: Root directory for catalog storage
        """
        self.base_path = Path(base_path)
        self.base_path.mkdir(parents=True, exist_ok=True)

    def save(
        self,
        catalog: DatabaseCatalog,
        format: str = "json",
    ) -> Path:
        """Save a database catalog to disk.

        Args:
            catalog: DatabaseCatalog to save
            format: Output format - "json" or "yaml"

        Returns:
            Path to the saved file

        Raises:
            CatalogError: If save fails
        """
        db_dir = self.base_path / catalog.database_name
        db_dir.mkdir(parents=True, exist_ok=True)

        ext = "yaml" if format == "yaml" else "json"
        file_path = db_dir / f"catalog.{ext}"

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

            self._update_index(catalog, str(file_path.relative_to(self.base_path)))

            logger.info(
                f"Catalog saved: {catalog.database_name} "
                f"({format}, {catalog.table_count} tables)"
            )

            return file_path

        except Exception as e:
            raise CatalogError(f"Failed to save catalog: {e}") from e

    def load(self, db_name: str) -> DatabaseCatalog | None:
        """Load a database catalog from disk.

        Tries JSON first, then YAML.

        Args:
            db_name: Database name

        Returns:
            DatabaseCatalog or None if not found
        """
        db_dir = self.base_path / db_name

        if not db_dir.exists():
            return None

        for ext in ("json", "yaml"):
            file_path = db_dir / f"catalog.{ext}"
            if file_path.exists():
                try:
                    return self._load_file(file_path)
                except Exception as e:
                    logger.warning(
                        f"Failed to load catalog file {file_path}: {e}"
                    )

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
        db_dir = self.base_path / db_name
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

    def save_as_draft(
        self,
        catalog: DatabaseCatalog,
        format: str = "json",
    ) -> Path:
        """Save catalog explicitly as a draft.

        Sets status to draft on catalog and all tables to pending.
        """
        catalog.status = CatalogStatus.draft
        for table in catalog.tables.values():
            table.review_status = TableReviewStatus.pending
        catalog.updated_at = datetime.now(UTC)
        return self.save(catalog, format=format)

    def approve_table(
        self,
        db_name: str,
        table_name: str,
    ) -> DatabaseCatalog:
        """Mark a single table as approved and save.

        Args:
            db_name: Database name
            table_name: Table to approve

        Returns:
            Updated DatabaseCatalog

        Raises:
            CatalogNotFoundError: If catalog not found
            TableNotFoundInCatalogError: If table not found
        """
        catalog = self.load_or_raise(db_name)
        table = catalog.get_table(table_name)
        if table is None:
            raise TableNotFoundInCatalogError(table_name, db_name)

        table.review_status = TableReviewStatus.approved
        catalog.updated_at = datetime.now(UTC)
        self.save(catalog)
        return catalog

    def approve_catalog(
        self,
        db_name: str,
    ) -> DatabaseCatalog:
        """Approve all tables and finalize the catalog.

        Sets catalog status to approved. All pending tables become approved.

        Returns:
            Updated DatabaseCatalog
        """
        catalog = self.load_or_raise(db_name)
        for table in catalog.tables.values():
            if table.review_status == TableReviewStatus.pending:
                table.review_status = TableReviewStatus.approved
        catalog.status = CatalogStatus.approved
        catalog.updated_at = datetime.now(UTC)
        self.save(catalog)
        return catalog

    def update_table_fields(
        self,
        db_name: str,
        table_name: str,
        updates: dict[str, Any],
        lang: str = "en",
    ) -> TableCatalogEntry:
        """Update editable fields on a table entry.

        Supports updating: description, human_name, tags, relationships.
        Marks the table as 'modified'.

        Args:
            db_name: Database name
            table_name: Table to update
            updates: Dict of field names to new values
            lang: Language for LocalizedText fields

        Returns:
            Updated TableCatalogEntry
        """
        catalog = self.load_or_raise(db_name)
        table = catalog.get_table(table_name)
        if table is None:
            raise TableNotFoundInCatalogError(table_name, db_name)

        if "description" in updates:
            val = updates["description"]
            if isinstance(val, str):
                table.description.set(lang, val)
                table.user_overrides.setdefault("description", {})[lang] = val
            elif isinstance(val, dict):
                for lang_code, text in val.items():
                    table.description.set(lang_code, text)
                table.user_overrides["description"] = {
                    **table.user_overrides.get("description", {}),
                    **val,
                }

        if "human_name" in updates:
            val = updates["human_name"]
            if isinstance(val, str):
                table.human_name.set(lang, val)
                table.user_overrides.setdefault("human_name", {})[lang] = val
            elif isinstance(val, dict):
                for lang_code, text in val.items():
                    table.human_name.set(lang_code, text)
                table.user_overrides["human_name"] = {
                    **table.user_overrides.get("human_name", {}),
                    **val,
                }

        if "tags" in updates:
            table.tags = updates["tags"]
            table.user_overrides["tags"] = updates["tags"]

        if "relationships" in updates:
            table.relationships = [
                RelationshipInfo(**r) if isinstance(r, dict) else r
                for r in updates["relationships"]
            ]
            table.user_overrides["relationships"] = updates["relationships"]

        table.review_status = TableReviewStatus.modified
        catalog.updated_at = datetime.now(UTC)
        self.save(catalog)
        return table

    def update_column_fields(
        self,
        db_name: str,
        table_name: str,
        column_name: str,
        updates: dict[str, Any],
        lang: str = "en",
    ) -> ColumnCatalogEntry:
        """Update editable fields on a column entry.

        Supports updating: description, semantic_type, tags.
        Marks the parent table as 'modified'.

        Returns:
            Updated ColumnCatalogEntry
        """
        catalog = self.load_or_raise(db_name)
        table = catalog.get_table(table_name)
        if table is None:
            raise TableNotFoundInCatalogError(table_name, db_name)

        column = table.get_column(column_name)
        if column is None:
            raise ColumnNotFoundInCatalogError(column_name, table_name, db_name)

        if "description" in updates:
            val = updates["description"]
            if isinstance(val, str):
                column.description.set(lang, val)
                column.user_overrides.setdefault("description", {})[lang] = val
            elif isinstance(val, dict):
                for lang_code, text in val.items():
                    column.description.set(lang_code, text)
                column.user_overrides["description"] = {
                    **column.user_overrides.get("description", {}),
                    **val,
                }

        if "semantic_type" in updates:
            column.semantic_type = updates["semantic_type"]
            column.user_overrides["semantic_type"] = updates["semantic_type"]

        if "tags" in updates:
            column.tags = updates["tags"]
            column.user_overrides["tags"] = updates["tags"]

        table.review_status = TableReviewStatus.modified
        catalog.updated_at = datetime.now(UTC)
        self.save(catalog)
        return column

    def extract_overrides(self, db_name: str) -> dict[str, dict[str, Any]]:
        """Extract all user_overrides from an existing catalog.

        Returns a dict keyed by table_name, each containing:
        - table-level overrides (description, human_name, tags, relationships)
        - columns: dict keyed by column_name with column-level overrides

        Used before regeneration to preserve user edits.
        """
        catalog = self.load(db_name)
        if not catalog:
            return {}

        overrides: dict[str, dict[str, Any]] = {}
        for tname, table in catalog.tables.items():
            table_overrides: dict[str, Any] = {}

            if table.user_overrides:
                table_overrides.update(table.user_overrides)

            col_overrides: dict[str, dict[str, Any]] = {}
            for col in table.columns:
                if col.user_overrides:
                    col_overrides[col.name] = dict(col.user_overrides)

            if col_overrides:
                table_overrides["columns"] = col_overrides

            if table_overrides:
                overrides[tname] = table_overrides

        return overrides

    def apply_overrides(
        self,
        db_name: str,
        overrides: dict[str, dict[str, Any]],
    ) -> DatabaseCatalog:
        """Apply saved user_overrides to a (newly regenerated) catalog.

        For each table in overrides, applies table-level and column-level
        user edits on top of the fresh LLM-generated values.

        Returns:
            Updated catalog with overrides applied.
        """
        catalog = self.load_or_raise(db_name)

        for tname, table_overrides in overrides.items():
            table = catalog.get_table(tname)
            if table is None:
                continue  # Table no longer exists in new schema

            # Apply table-level overrides
            if "description" in table_overrides:
                desc_overrides = table_overrides["description"]
                if isinstance(desc_overrides, dict):
                    for lang, text in desc_overrides.items():
                        table.description.set(lang, text)

            if "human_name" in table_overrides:
                hn_overrides = table_overrides["human_name"]
                if isinstance(hn_overrides, dict):
                    for lang, text in hn_overrides.items():
                        table.human_name.set(lang, text)

            if "tags" in table_overrides:
                table.tags = table_overrides["tags"]

            if "relationships" in table_overrides and table_overrides["relationships"]:
                table.relationships = [
                    RelationshipInfo(**r) if isinstance(r, dict) else r
                    for r in table_overrides["relationships"]
                ]

            # Restore table user_overrides (without columns key)
            table.user_overrides = {
                k: v for k, v in table_overrides.items() if k != "columns"
            }
            if any(k != "columns" for k in table_overrides):
                table.review_status = TableReviewStatus.modified

            # Apply column-level overrides
            col_overrides = table_overrides.get("columns", {})
            for col_name, col_updates in col_overrides.items():
                column = table.get_column(col_name)
                if column is None:
                    continue  # Column no longer exists

                if "description" in col_updates:
                    desc = col_updates["description"]
                    if isinstance(desc, dict):
                        for lang, text in desc.items():
                            column.description.set(lang, text)

                if "semantic_type" in col_updates:
                    column.semantic_type = col_updates["semantic_type"]

                if "tags" in col_updates:
                    column.tags = col_updates["tags"]

                column.user_overrides = dict(col_updates)

        catalog.updated_at = datetime.now(UTC)
        self.save(catalog)
        return catalog

    def list_drafts(self) -> list[str]:
        """List catalogs that are in draft status."""
        drafts = []
        for name in self.list_catalogs():
            catalog = self.load(name)
            if catalog and catalog.status == CatalogStatus.draft:
                drafts.append(name)
        return drafts

    # --- Cross-reference methods ---

    def find_similar_tables(
        self,
        table_name: str,
        exclude_db: str | None = None,
    ) -> list[tuple[str, TableCatalogEntry]]:
        """Find tables with similar names across all catalogs."""
        results = []
        normalized = self._normalize_name(table_name)

        for db_name in self.list_catalogs():
            if db_name == exclude_db:
                continue

            catalog = self.load(db_name)
            if not catalog:
                continue

            for tname, table in catalog.tables.items():
                if self._names_are_similar(normalized, self._normalize_name(tname)):
                    results.append((db_name, table))

        return results

    def find_similar_columns(
        self,
        column_name: str,
        data_type: str | None = None,
        exclude_db: str | None = None,
    ) -> list[tuple[str, str, ColumnCatalogEntry]]:
        """Find columns with similar names across all catalogs."""
        results = []
        normalized = self._normalize_name(column_name)

        for db_name in self.list_catalogs():
            if db_name == exclude_db:
                continue

            catalog = self.load(db_name)
            if not catalog:
                continue

            for tname, table in catalog.tables.items():
                for col in table.columns:
                    if self._names_are_similar(
                        normalized, self._normalize_name(col.name)
                    ):
                        if data_type and col.data_type != data_type:
                            continue
                        results.append((db_name, tname, col))

        return results

    def build_cross_reference_context(
        self,
        table_name: str,
        column_names: list[str],
        exclude_db: str | None = None,
    ) -> str:
        """Build cross-reference context string for LLM prompts."""
        parts = []

        similar_tables = self.find_similar_tables(table_name, exclude_db=exclude_db)
        if similar_tables:
            parts.append("Previously analyzed similar tables:")
            for db_name, table in similar_tables[:3]:
                desc = str(table.description) if not table.description.is_empty else "N/A"
                parts.append(f"  - {db_name}.{table.table_name}: {desc}")

        for col_name in column_names[:10]:
            similar_cols = self.find_similar_columns(
                col_name, exclude_db=exclude_db
            )
            if similar_cols:
                for db_name, tname, col in similar_cols[:2]:
                    if col.semantic_type or not col.description.is_empty:
                        desc = str(col.description) if not col.description.is_empty else ""
                        sem = f" [semantic: {col.semantic_type}]" if col.semantic_type else ""
                        parts.append(
                            f"  - {db_name}.{tname}.{col.name}: {desc}{sem}"
                        )

        if not parts:
            return "No previous catalog data available for cross-reference."

        return "\n".join(parts)

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

    @staticmethod
    def _normalize_name(name: str) -> str:
        """Normalize a table/column name for comparison."""
        n = name.lower().strip()
        for prefix in ("tbl_", "t_", "tb_", "dim_", "fact_"):
            if n.startswith(prefix):
                n = n[len(prefix):]
                break
        for suffix in ("_id", "_key", "_fk", "_pk"):
            if n.endswith(suffix):
                n = n[: -len(suffix)]
                break
        if n.endswith("ies"):
            n = n[:-3] + "y"
        elif n.endswith("ses"):
            n = n[:-2]
        elif n.endswith("s") and not n.endswith("ss"):
            n = n[:-1]
        return n

    @staticmethod
    def _names_are_similar(name1: str, name2: str) -> bool:
        """Check if two normalized names are similar."""
        if name1 == name2:
            return True
        return bool(len(name1) >= 3 and len(name2) >= 3 and (name1 in name2 or name2 in name1))
