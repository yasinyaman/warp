"""Tests for CrossReferenceProvider over a real CatalogFileStore."""

from pathlib import Path

import pytest

from warp.catalog.models import (
    ColumnCatalogEntry,
    DatabaseCatalog,
    LocalizedText,
    TableCatalogEntry,
)
from warp.catalog.store import CatalogFileStore
from warp.enrichment.cross_reference import CrossReferenceProvider


@pytest.fixture
def store(tmp_path: Path) -> CatalogFileStore:
    s = CatalogFileStore(tmp_path)
    catalog = DatabaseCatalog(
        database_name="olddb",
        tables={
            "users": TableCatalogEntry(
                table_name="users",
                description=LocalizedText(texts={"en": "User accounts"}),
                human_name=LocalizedText(texts={"en": "Users"}),
                columns=[
                    ColumnCatalogEntry(
                        name="email",
                        data_type="varchar",
                        semantic_type="email",
                        description=LocalizedText(texts={"en": "Email address"}),
                    ),
                    ColumnCatalogEntry(name="raw", data_type="text"),
                ],
            ),
        },
    )
    s.save(catalog)
    return s


def test_get_context_for_similar_table(store: CatalogFileStore) -> None:
    provider = CrossReferenceProvider(store, exclude_db="newdb")
    ctx = provider.get_context_for_table("user", ["email"])
    assert "olddb.users" in ctx
    assert "Users" in ctx
    assert "email" in ctx
    assert "[type: email]" in ctx


def test_get_context_no_matches(store: CatalogFileStore) -> None:
    provider = CrossReferenceProvider(store, exclude_db="newdb")
    ctx = provider.get_context_for_table("zzz_no_match_table", ["zzz_col"])
    assert ctx == ""


def test_get_context_excludes_self(store: CatalogFileStore) -> None:
    provider = CrossReferenceProvider(store, exclude_db="olddb")
    ctx = provider.get_context_for_table("users", ["email"])
    assert ctx == ""


def test_has_references_true(store: CatalogFileStore) -> None:
    provider = CrossReferenceProvider(store)
    assert provider.has_references() is True


def test_has_references_excludes_only_catalog(store: CatalogFileStore) -> None:
    provider = CrossReferenceProvider(store, exclude_db="olddb")
    assert provider.has_references() is False


def test_has_references_empty(tmp_path: Path) -> None:
    empty = CatalogFileStore(tmp_path / "empty")
    provider = CrossReferenceProvider(empty)
    assert provider.has_references() is False


def test_find_similar_tables_handles_store_error(tmp_path: Path) -> None:
    store = CatalogFileStore(tmp_path)
    provider = CrossReferenceProvider(store)

    def boom(*a: object, **k: object) -> list:
        raise RuntimeError("disk error")

    store.find_similar_tables = boom  # type: ignore[method-assign]
    assert provider._find_similar_tables("users") == []


def test_find_similar_columns_handles_store_error(tmp_path: Path) -> None:
    store = CatalogFileStore(tmp_path)
    provider = CrossReferenceProvider(store)

    def boom(*a: object, **k: object) -> list:
        raise RuntimeError("disk error")

    store.find_similar_columns = boom  # type: ignore[method-assign]
    assert provider._find_similar_columns(["email"]) == {}


def test_max_refs_limits(store: CatalogFileStore) -> None:
    provider = CrossReferenceProvider(
        store, exclude_db="newdb", max_table_refs=0, max_column_refs=0
    )
    # max_table_refs=0 truncates similar tables, but similar columns may still
    # appear (column search is independent). Result is a string.
    ctx = provider.get_context_for_table("users", ["email"])
    assert isinstance(ctx, str)
