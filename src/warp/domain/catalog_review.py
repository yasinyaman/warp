"""Pure review-workflow transitions on a `DatabaseCatalog`.

Every function mutates the catalog it is given and returns it (or the touched
entry) without touching storage; the application service loads, applies and
persists.
"""

from datetime import UTC, datetime
from typing import Any

from warp.domain.catalog import (
    CatalogStatus,
    ColumnCatalogEntry,
    DatabaseCatalog,
    RelationshipInfo,
    TableCatalogEntry,
    TableReviewStatus,
)
from warp.domain.errors import ColumnNotFoundInCatalogError, TableNotFoundInCatalogError


def _touch(catalog: DatabaseCatalog) -> None:
    catalog.updated_at = datetime.now(UTC)


def _require_table(catalog: DatabaseCatalog, table_name: str) -> TableCatalogEntry:
    table = catalog.get_table(table_name)
    if table is None:
        raise TableNotFoundInCatalogError(table_name, catalog.database_name)
    return table


def mark_as_draft(catalog: DatabaseCatalog) -> DatabaseCatalog:
    """Reset a catalog to draft with every table pending review."""
    catalog.status = CatalogStatus.draft
    for table in catalog.tables.values():
        table.review_status = TableReviewStatus.pending
    _touch(catalog)
    return catalog


def approve_table(catalog: DatabaseCatalog, table_name: str) -> DatabaseCatalog:
    """Mark one table as approved.

    Raises:
        TableNotFoundInCatalogError: If the table is not in the catalog.
    """
    _require_table(catalog, table_name).review_status = TableReviewStatus.approved
    _touch(catalog)
    return catalog


def approve_catalog(catalog: DatabaseCatalog) -> DatabaseCatalog:
    """Approve every pending table and finalize the catalog."""
    for table in catalog.tables.values():
        if table.review_status == TableReviewStatus.pending:
            table.review_status = TableReviewStatus.approved
    catalog.status = CatalogStatus.approved
    _touch(catalog)
    return catalog


def _set_localized(entry: Any, field_name: str, value: Any, lang: str) -> None:
    localized = getattr(entry, field_name)
    if isinstance(value, str):
        localized.set(lang, value)
        entry.user_overrides.setdefault(field_name, {})[lang] = value
    elif isinstance(value, dict):
        for lang_code, text in value.items():
            localized.set(lang_code, text)
        entry.user_overrides[field_name] = {**entry.user_overrides.get(field_name, {}), **value}


def update_table_fields(
    catalog: DatabaseCatalog, table_name: str, updates: dict[str, Any], lang: str = "en"
) -> TableCatalogEntry:
    """Apply user edits (description, human_name, tags, relationships) to a table.

    The edits are recorded in `user_overrides` so they survive re-analysis and
    the table is marked `modified`.
    """
    table = _require_table(catalog, table_name)

    if "description" in updates:
        _set_localized(table, "description", updates["description"], lang)
    if "human_name" in updates:
        _set_localized(table, "human_name", updates["human_name"], lang)
    if "tags" in updates:
        table.tags = updates["tags"]
        table.user_overrides["tags"] = updates["tags"]
    if "relationships" in updates:
        table.relationships = [
            RelationshipInfo(**r) if isinstance(r, dict) else r for r in updates["relationships"]
        ]
        table.user_overrides["relationships"] = updates["relationships"]

    table.review_status = TableReviewStatus.modified
    _touch(catalog)
    return table


def update_column_fields(
    catalog: DatabaseCatalog,
    table_name: str,
    column_name: str,
    updates: dict[str, Any],
    lang: str = "en",
) -> ColumnCatalogEntry:
    """Apply user edits (description, semantic_type, tags) to a column.

    Raises:
        ColumnNotFoundInCatalogError: If the column is not in the table.
    """
    table = _require_table(catalog, table_name)
    column = table.get_column(column_name)
    if column is None:
        raise ColumnNotFoundInCatalogError(column_name, table_name, catalog.database_name)

    if "description" in updates:
        _set_localized(column, "description", updates["description"], lang)
    if "semantic_type" in updates:
        column.semantic_type = updates["semantic_type"]
        column.user_overrides["semantic_type"] = updates["semantic_type"]
    if "tags" in updates:
        column.tags = updates["tags"]
        column.user_overrides["tags"] = updates["tags"]

    table.review_status = TableReviewStatus.modified
    _touch(catalog)
    return column


def extract_overrides(catalog: DatabaseCatalog) -> dict[str, dict[str, Any]]:
    """Collect every `user_overrides` entry, keyed by table (columns nested)."""
    overrides: dict[str, dict[str, Any]] = {}
    for tname, table in catalog.tables.items():
        table_overrides: dict[str, Any] = dict(table.user_overrides)
        col_overrides = {c.name: dict(c.user_overrides) for c in table.columns if c.user_overrides}
        if col_overrides:
            table_overrides["columns"] = col_overrides
        if table_overrides:
            overrides[tname] = table_overrides
    return overrides


def apply_overrides(  # noqa: C901, PLR0912
    catalog: DatabaseCatalog, overrides: dict[str, dict[str, Any]]
) -> DatabaseCatalog:
    """Re-apply saved user edits on top of a freshly generated catalog.

    Tables/columns that no longer exist are skipped; edited tables become
    `modified`.
    """
    for tname, table_overrides in overrides.items():
        table = catalog.get_table(tname)
        if table is None:
            continue

        for field_name in ("description", "human_name"):
            values = table_overrides.get(field_name)
            if isinstance(values, dict):
                for lang, text in values.items():
                    getattr(table, field_name).set(lang, text)
        if "tags" in table_overrides:
            table.tags = table_overrides["tags"]
        if table_overrides.get("relationships"):
            table.relationships = [
                RelationshipInfo(**r) if isinstance(r, dict) else r
                for r in table_overrides["relationships"]
            ]

        table.user_overrides = {k: v for k, v in table_overrides.items() if k != "columns"}
        if any(k != "columns" for k in table_overrides):
            table.review_status = TableReviewStatus.modified

        for col_name, col_updates in table_overrides.get("columns", {}).items():
            column = table.get_column(col_name)
            if column is None:
                continue
            desc = col_updates.get("description")
            if isinstance(desc, dict):
                for lang, text in desc.items():
                    column.description.set(lang, text)
            if "semantic_type" in col_updates:
                column.semantic_type = col_updates["semantic_type"]
            if "tags" in col_updates:
                column.tags = col_updates["tags"]
            column.user_overrides = dict(col_updates)

    _touch(catalog)
    return catalog
