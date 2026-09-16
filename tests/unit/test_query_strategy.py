"""Tests for QueryStrategy (catalog-aware SQL generation)."""

from unittest.mock import AsyncMock

import pytest

from warp.catalog.models import (
    ColumnCatalogEntry,
    DatabaseCatalog,
    LocalizedText,
    RelationshipInfo,
    TableCatalogEntry,
)
from warp.query.strategy import QueryStrategy


def _catalog() -> DatabaseCatalog:
    return DatabaseCatalog(
        database_name="shop",
        database_type="postgresql",
        tables={
            "users": TableCatalogEntry(
                table_name="users",
                description=LocalizedText(texts={"en": "All users"}),
                human_name=LocalizedText(texts={"en": "Users"}),
                columns=[
                    ColumnCatalogEntry(
                        name="id", data_type="integer", is_primary_key=True, nullable=False
                    ),
                    ColumnCatalogEntry(
                        name="email",
                        data_type="varchar",
                        semantic_type="email",
                        nullable=False,
                    ),
                    ColumnCatalogEntry(
                        name="org_id",
                        data_type="integer",
                        is_foreign_key=True,
                        references="orgs.id",
                    ),
                ],
                primary_key="id",
                row_count=500,
                relationships=[
                    RelationshipInfo(
                        source_column="org_id",
                        target_table="orgs",
                        target_column="id",
                        description=LocalizedText(texts={"en": "belongs to org"}),
                    )
                ],
            ),
        },
    )


@pytest.mark.asyncio
async def test_generate_query_parses_json() -> None:
    llm = AsyncMock()
    llm.generate_json = AsyncMock(
        return_value='{"query": "SELECT 1", "explanation": "x", "tables_used": ["users"], "notes": ""}'
    )
    strat = QueryStrategy(_catalog(), llm)
    result = await strat.generate_query("count users")
    assert result["query"] == "SELECT 1"
    assert result["tables_used"] == ["users"]
    # The schema context with table/column/rel info is sent to the LLM.
    prompt = llm.generate_json.call_args.kwargs["prompt"]
    assert "users" in prompt
    assert "email" in prompt
    assert "REL:" in prompt


@pytest.mark.asyncio
async def test_generate_query_invalid_json_fallback() -> None:
    llm = AsyncMock()
    llm.generate_json = AsyncMock(return_value="not json at all")
    strat = QueryStrategy(_catalog(), llm, lang="tr")
    result = await strat.generate_query("show me")
    assert result["query"] == "not json at all"
    assert result["tables_used"] == []
    assert "JSON parsing failed" in result["explanation"]


def test_build_schema_context_contents() -> None:
    strat = QueryStrategy(_catalog(), AsyncMock())
    ctx = strat._build_schema_context()
    assert "Table: users (Users) - All users" in ctx
    assert "~500 rows" in ctx
    assert "PK" in ctx
    assert "FK->orgs.id" in ctx
    assert "[email]" in ctx
    assert "NOT NULL" in ctx
