"""Cross-reference module for catalog enrichment.

Provides context from previously analyzed catalogs when generating
descriptions for new databases/tables.
"""

import logging
from typing import Any

from warp.application.ports.catalog_repository import CatalogRepository
from warp.domain.catalog import ColumnCatalogEntry, TableCatalogEntry
from warp.domain.catalog_naming import names_are_similar, normalize_name

logger = logging.getLogger(__name__)


class CrossReferenceService:
    """Finds related tables/columns in other catalogs and renders prompt context."""

    def __init__(
        self,
        repository: CatalogRepository,
        exclude_db: str | None = None,
        max_table_refs: int = 3,
        max_column_refs: int = 2,
    ):
        """Store the repository and cross-reference limits."""
        self.repository = repository
        self.exclude_db = exclude_db
        self.max_table_refs = max_table_refs
        self.max_column_refs = max_column_refs

    # -- search -----------------------------------------------------------------------
    def _catalogs(self) -> list[tuple[str, Any]]:
        out = []
        for db_name in self.repository.list_catalogs():
            if db_name == self.exclude_db:
                continue
            catalog = self.repository.load(db_name)
            if catalog:
                out.append((db_name, catalog))
        return out

    def find_similar_tables(self, table_name: str) -> list[tuple[str, TableCatalogEntry]]:
        """Tables whose normalized name matches `table_name` across other catalogs."""
        normalized = normalize_name(table_name)
        return [
            (db_name, table)
            for db_name, catalog in self._catalogs()
            for tname, table in catalog.tables.items()
            if names_are_similar(normalized, normalize_name(tname))
        ]

    def find_similar_columns(
        self, column_name: str, data_type: str | None = None
    ) -> list[tuple[str, str, ColumnCatalogEntry]]:
        """Columns whose normalized name matches `column_name` across other catalogs."""
        normalized = normalize_name(column_name)
        return [
            (db_name, tname, col)
            for db_name, catalog in self._catalogs()
            for tname, table in catalog.tables.items()
            for col in table.columns
            if names_are_similar(normalized, normalize_name(col.name))
            and (data_type is None or col.data_type == data_type)
        ]

    def get_context_for_table(
        self,
        table_name: str,
        column_names: list[str],
    ) -> str:
        """Get cross-reference context for a table."""
        parts: list[str] = []

        similar_tables = self._find_similar_tables(table_name)
        if similar_tables:
            parts.append("Previously analyzed similar tables:")
            for db_name, table in similar_tables:
                desc = str(table.description) if not table.description.is_empty else ""
                human = str(table.human_name) if not table.human_name.is_empty else ""
                line = f"  - {db_name}.{table.table_name}"
                if human:
                    line += f" ({human})"
                if desc:
                    line += f": {desc}"
                parts.append(line)

                for col in table.columns:
                    if not col.description.is_empty or col.semantic_type:
                        col_desc = str(col.description) if not col.description.is_empty else ""
                        sem = f" [type: {col.semantic_type}]" if col.semantic_type else ""
                        parts.append(f"    - {col.name}: {col_desc}{sem}")

        column_refs = self._find_similar_columns(column_names)
        if column_refs:
            parts.append("Previously analyzed similar columns:")
            for _col_name, refs in column_refs.items():
                for db_name, tname, col in refs:
                    desc = str(col.description) if not col.description.is_empty else ""
                    sem = f" [type: {col.semantic_type}]" if col.semantic_type else ""
                    parts.append(f"  - {db_name}.{tname}.{col.name}: {desc}{sem}")

        if not parts:
            return ""

        return "\n".join(parts)

    def _find_similar_tables(self, table_name: str) -> list[tuple[str, TableCatalogEntry]]:
        """Find tables with similar names in existing catalogs."""
        try:
            return self.find_similar_tables(table_name)[: self.max_table_refs]
        except Exception as e:
            logger.warning(f"Cross-reference table search failed for {table_name}: {e}")
            return []

    def _find_similar_columns(
        self, column_names: list[str]
    ) -> dict[str, list[tuple[str, str, ColumnCatalogEntry]]]:
        """Find columns with similar names in existing catalogs."""
        result: dict[str, list[tuple[str, str, ColumnCatalogEntry]]] = {}

        for col_name in column_names[:15]:
            try:
                refs = self.find_similar_columns(col_name)
                useful_refs = [
                    r for r in refs if not r[2].description.is_empty or r[2].semantic_type
                ]
                if useful_refs:
                    result[col_name] = useful_refs[: self.max_column_refs]
            except Exception as e:
                logger.debug(f"Cross-reference column search failed for {col_name}: {e}")

        return result

    def has_references(self) -> bool:
        """Check if any catalogs exist for cross-referencing."""
        catalogs = self.repository.list_catalogs()
        if self.exclude_db:
            catalogs = [c for c in catalogs if c != self.exclude_db]
        return len(catalogs) > 0
