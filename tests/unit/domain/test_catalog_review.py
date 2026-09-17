"""Tests for the pure review transitions."""

import pytest

from warp.domain import catalog_review as rules
from warp.domain.catalog import (
    CatalogStatus,
    ColumnCatalogEntry,
    DatabaseCatalog,
    LocalizedText,
    TableCatalogEntry,
    TableReviewStatus,
)
from warp.domain.errors import ColumnNotFoundInCatalogError, TableNotFoundInCatalogError


def _catalog() -> DatabaseCatalog:
    return DatabaseCatalog(
        database_name="db",
        tables={
            "users": TableCatalogEntry(
                table_name="users",
                description=LocalizedText(texts={"en": "Users"}),
                columns=[ColumnCatalogEntry(name="email", data_type="varchar")],
                review_status=TableReviewStatus.approved,
            ),
            "orders": TableCatalogEntry(table_name="orders"),
        },
        status=CatalogStatus.approved,
    )


def test_mark_as_draft_resets_everything():
    c = rules.mark_as_draft(_catalog())
    assert c.status == CatalogStatus.draft
    assert {t.review_status for t in c.tables.values()} == {TableReviewStatus.pending}


def test_approve_table_and_catalog():
    c = rules.mark_as_draft(_catalog())
    rules.approve_table(c, "users")
    assert c.tables["users"].review_status == TableReviewStatus.approved
    assert c.tables["orders"].review_status == TableReviewStatus.pending
    rules.approve_catalog(c)
    assert c.status == CatalogStatus.approved
    assert c.tables["orders"].review_status == TableReviewStatus.approved
    with pytest.raises(TableNotFoundInCatalogError):
        rules.approve_table(c, "nope")


def test_update_fields_record_overrides():
    c = rules.mark_as_draft(_catalog())
    table = rules.update_table_fields(c, "users", {"description": "People", "tags": ["core"]}, "en")
    assert table.description.get("en") == "People"
    assert table.user_overrides == {"description": {"en": "People"}, "tags": ["core"]}
    assert table.review_status == TableReviewStatus.modified

    col = rules.update_column_fields(c, "users", "email", {"semantic_type": "email"})
    assert col.user_overrides == {"semantic_type": "email"}
    with pytest.raises(ColumnNotFoundInCatalogError):
        rules.update_column_fields(c, "users", "nope", {"tags": []})


def test_overrides_round_trip():
    edited = rules.mark_as_draft(_catalog())
    rules.update_table_fields(edited, "users", {"human_name": {"en": "Members"}})
    rules.update_column_fields(edited, "users", "email", {"tags": ["pii"]})
    overrides = rules.extract_overrides(edited)
    assert overrides == {
        "users": {"human_name": {"en": "Members"}, "columns": {"email": {"tags": ["pii"]}}}
    }

    fresh = rules.mark_as_draft(_catalog())
    fresh.tables.pop("orders")  # a table that disappeared is skipped silently
    rules.apply_overrides(fresh, {**overrides, "orders": {"tags": ["gone"]}})
    assert fresh.tables["users"].human_name.get("en") == "Members"
    assert fresh.tables["users"].review_status == TableReviewStatus.modified
    assert fresh.tables["users"].columns[0].tags == ["pii"]
    assert rules.extract_overrides(_catalog()) == {}
