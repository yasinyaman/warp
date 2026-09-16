"""Tests for SampleReader, PII masking, and prompt builders."""

from typing import Any
from unittest.mock import AsyncMock

import pytest

from warp.adapters.outbound.db.sample_reader import (
    ColumnStats,
    SampleReader,
    TableSamples,
    is_pii_column,
    mask_pii_samples,
)
from warp.application import prompts


class FakeAdapter:
    def __init__(self) -> None:
        self.execute_query = AsyncMock(return_value=[])


# --- PII helpers ---


def test_is_pii_column() -> None:
    assert is_pii_column("user_email") is True
    assert is_pii_column("PhoneNumber") is True
    assert is_pii_column("username") is False


def test_is_pii_column_custom_patterns() -> None:
    assert is_pii_column("foo_bar", patterns=["bar"]) is True
    assert is_pii_column("foo", patterns=["bar"]) is False


def test_mask_pii_samples() -> None:
    samples = TableSamples(
        table_name="users",
        column_samples={
            "email": ["a@x.com", "b@x.com"],
            "name": ["Alice", "Bob"],
        },
        column_stats={
            "email": ColumnStats(column_name="email", sample_values=["a@x.com"]),
            "name": ColumnStats(column_name="name", sample_values=["Alice"]),
        },
    )
    masked = mask_pii_samples(samples)
    assert masked.column_samples["email"] == ["***", "***"]
    assert masked.column_samples["name"] == ["Alice", "Bob"]
    assert masked.column_stats["email"].sample_values == ["***"]
    assert masked.column_stats["name"].sample_values == ["Alice"]
    # original is not mutated
    assert samples.column_samples["email"] == ["a@x.com", "b@x.com"]


# --- SampleReader ---


@pytest.mark.asyncio
async def test_read_samples_pg() -> None:
    adapter = FakeAdapter()
    adapter.execute_query.return_value = [
        {"id": 1, "name": "Alice"},
        {"id": 2, "name": "Bob"},
    ]
    reader = SampleReader(adapter, db_type="postgresql", schema="public")
    result = await reader.read_samples("users", limit=2)
    assert result == {"id": [1, 2], "name": ["Alice", "Bob"]}
    query = adapter.execute_query.call_args.args[0]
    assert '"public"."users"' in query


@pytest.mark.asyncio
async def test_read_samples_mysql_no_schema_prefix() -> None:
    adapter = FakeAdapter()
    adapter.execute_query.return_value = [{"id": 1}]
    reader = SampleReader(adapter, db_type="mysql", schema="db")
    await reader.read_samples("users")
    query = adapter.execute_query.call_args.args[0]
    assert "`users`" in query
    assert "`db`." not in query


@pytest.mark.asyncio
async def test_read_samples_empty() -> None:
    adapter = FakeAdapter()
    adapter.execute_query.return_value = []
    reader = SampleReader(adapter)
    assert await reader.read_samples("users") == {}


@pytest.mark.asyncio
async def test_read_samples_error_returns_empty() -> None:
    adapter = FakeAdapter()
    adapter.execute_query.side_effect = RuntimeError("boom")
    reader = SampleReader(adapter)
    assert await reader.read_samples("users") == {}


@pytest.mark.asyncio
async def test_read_samples_invalid_identifier() -> None:
    adapter = FakeAdapter()
    reader = SampleReader(adapter, db_type="postgresql")
    # Crafted name fails sanitizer -> caught -> {}
    assert await reader.read_samples('users"; DROP TABLE x;--') == {}


@pytest.mark.asyncio
async def test_read_row_count_pg() -> None:
    adapter = FakeAdapter()
    adapter.execute_query.return_value = [{"row_count": 1000}]
    reader = SampleReader(adapter, db_type="postgresql")
    assert await reader.read_row_count("users") == 1000


@pytest.mark.asyncio
async def test_read_row_count_pg_negative_clamped() -> None:
    adapter = FakeAdapter()
    adapter.execute_query.return_value = [{"row_count": -1}]
    reader = SampleReader(adapter, db_type="postgresql")
    assert await reader.read_row_count("users") == 0


@pytest.mark.asyncio
async def test_read_row_count_mysql() -> None:
    adapter = FakeAdapter()
    adapter.execute_query.return_value = [{"row_count": 50}]
    reader = SampleReader(adapter, db_type="mysql", schema="db")
    assert await reader.read_row_count("users") == 50


@pytest.mark.asyncio
async def test_read_row_count_none() -> None:
    adapter = FakeAdapter()
    adapter.execute_query.return_value = [{"row_count": None}]
    reader = SampleReader(adapter)
    assert await reader.read_row_count("users") is None


@pytest.mark.asyncio
async def test_read_row_count_error() -> None:
    adapter = FakeAdapter()
    adapter.execute_query.side_effect = RuntimeError("boom")
    reader = SampleReader(adapter)
    assert await reader.read_row_count("users") is None


@pytest.mark.asyncio
async def test_read_column_stats_explicit_columns() -> None:
    adapter = FakeAdapter()
    adapter.execute_query.return_value = [{"distinct_count": 5, "null_count": 1}]
    reader = SampleReader(adapter, db_type="postgresql")
    stats = await reader.read_column_stats("users", columns=["status"])
    assert stats["status"].distinct_count == 5
    assert stats["status"].null_count == 1


@pytest.mark.asyncio
async def test_read_column_stats_autodiscover_columns() -> None:
    adapter = FakeAdapter()
    # first call returns a sample row to discover columns; subsequent calls stats
    adapter.execute_query.side_effect = [
        [{"id": 1, "status": "active"}],
        [{"distinct_count": 1, "null_count": 0}],
        [{"distinct_count": 2, "null_count": 0}],
    ]
    reader = SampleReader(adapter, db_type="postgresql")
    stats = await reader.read_column_stats("users")
    assert set(stats.keys()) == {"id", "status"}


@pytest.mark.asyncio
async def test_read_column_stats_no_rows_for_autodiscover() -> None:
    adapter = FakeAdapter()
    adapter.execute_query.return_value = []
    reader = SampleReader(adapter, db_type="postgresql")
    assert await reader.read_column_stats("users") == {}


@pytest.mark.asyncio
async def test_read_column_stats_invalid_table() -> None:
    adapter = FakeAdapter()
    reader = SampleReader(adapter, db_type="postgresql")
    assert await reader.read_column_stats('bad"name', columns=["x"]) == {}


@pytest.mark.asyncio
async def test_read_column_stats_query_error_per_column() -> None:
    adapter = FakeAdapter()
    adapter.execute_query.side_effect = RuntimeError("fail")
    reader = SampleReader(adapter, db_type="postgresql")
    stats = await reader.read_column_stats("users", columns=["status"])
    assert stats["status"].distinct_count is None


@pytest.mark.asyncio
async def test_read_table_samples_full() -> None:
    adapter = FakeAdapter()
    adapter.execute_query.side_effect = [
        # read_samples
        [{"id": 1, "status": "a"}, {"id": 2, "status": "a"}],
        # read_row_count
        [{"row_count": 2}],
        # read_column_stats for id
        [{"distinct_count": 2, "null_count": 0}],
        # read_column_stats for status
        [{"distinct_count": 1, "null_count": 0}],
    ]
    reader = SampleReader(adapter, db_type="postgresql")
    samples = await reader.read_table_samples("users", sample_limit=5)
    assert samples.row_count == 2
    assert "id" in samples.column_samples
    # dedup applied to stats.sample_values
    assert samples.column_stats["status"].sample_values == ["a"]


def test_serialize_value() -> None:
    assert SampleReader._serialize_value(None) is None
    assert SampleReader._serialize_value(5) == 5
    assert SampleReader._serialize_value("x") == "x"
    assert SampleReader._serialize_value(True) is True

    class Custom:
        def __str__(self) -> str:
            return "custom"

    assert SampleReader._serialize_value(Custom()) == "custom"


# --- prompt builders ---


def test_format_columns_text() -> None:
    cols: list[dict[str, Any]] = [
        {
            "name": "id",
            "type": "int",
            "nullable": False,
            "key": "PRI",
            "default": None,
            "max_length": None,
        },
        {
            "name": "email",
            "full_type": "varchar(255)",
            "type": "varchar",
            "nullable": True,
            "default": "x",
            "max_length": 255,
        },
    ]
    text = prompts.format_columns_text(cols)
    assert "id: int" in text
    assert "NOT NULL" in text
    assert "PRIMARY KEY" in text
    assert "varchar(255)" in text
    assert "DEFAULT x" in text
    assert "max_length=255" in text


def test_format_columns_text_empty() -> None:
    assert prompts.format_columns_text([]) == "  (no columns)"


def test_format_foreign_keys_text() -> None:
    assert prompts.format_foreign_keys_text([]) == "  (none)"
    fks = [{"column": "u", "references_table": "users", "references_column": "id"}]
    assert "u -> users.id" in prompts.format_foreign_keys_text(fks)


def test_format_indexes_text() -> None:
    assert prompts.format_indexes_text([]) == "  (none)"
    idx = [{"name": "ix", "columns": ["a", "b"], "unique": True}]
    out = prompts.format_indexes_text(idx)
    assert "UNIQUE" in out and "(a, b)" in out


def test_format_db_comments_text() -> None:
    assert "none" in prompts.format_db_comments_text(None, {})
    out = prompts.format_db_comments_text("table cmt", {"c": "col cmt"})
    assert "table cmt" in out and "col cmt" in out


def test_format_sample_data_text_none() -> None:
    assert "none available" in prompts.format_sample_data_text(None)
    empty = TableSamples(table_name="t")
    assert "none available" in prompts.format_sample_data_text(empty)


def test_format_sample_data_text_with_data() -> None:
    samples = TableSamples(
        table_name="t",
        row_count=10,
        column_samples={"name": ["x" * 60, "short"]},
        column_stats={"name": ColumnStats(column_name="name", distinct_count=2, null_count=0)},
    )
    out = prompts.format_sample_data_text(samples)
    assert "row_count" in out
    assert "..." in out  # long value truncated
    assert "distinct=2" in out


def test_build_table_analysis_prompt() -> None:
    out = prompts.build_table_analysis_prompt(
        table_name="users",
        database_name="db",
        database_type="postgresql",
        columns=[{"name": "id", "type": "int"}],
        foreign_keys=[],
        indexes=[],
        cross_reference_context="some context",
        languages=["en", "tr"],
    )
    assert "Table: users" in out
    assert "Cross-reference from other catalogs" in out
    assert '"en": "..."' in out


def test_build_table_analysis_prompt_no_cross_ref() -> None:
    out = prompts.build_table_analysis_prompt(
        table_name="t",
        database_name="db",
        database_type="mysql",
        columns=[],
        foreign_keys=[],
        indexes=[],
    )
    assert "Cross-reference" not in out


def test_lang_format_single_and_multi() -> None:
    assert prompts._lang_format(["en"]) == '{"en": "description here"}'
    assert prompts._lang_format([]) == '{"en": "description here"}'
    multi = prompts._lang_format(["en", "tr"])
    assert '"en": "..."' in multi and '"tr": "..."' in multi


def test_build_system_prompt_single_lang() -> None:
    out = prompts.build_system_prompt(languages=["tr"])
    assert "tr" in out


def test_build_system_prompt_multi_lang() -> None:
    out = prompts.build_system_prompt(languages=["en", "tr"])
    assert "these languages" in out


def test_build_system_prompt_explicit_instruction() -> None:
    out = prompts.build_system_prompt(language_instruction="custom inst")
    assert "custom inst" in out


def test_build_translation_prompt() -> None:
    out = prompts.build_translation_prompt("hello", "en", "tr")
    assert "hello" in out and "en" in out and "tr" in out


@pytest.mark.asyncio
async def test_read_row_count_binds_named_params_pg() -> None:
    adapter = FakeAdapter()
    adapter.execute_query.return_value = [{"row_count": 10}]
    reader = SampleReader(adapter, db_type="postgresql", schema="public")
    assert await reader.read_row_count("users") == 10
    sql, params = adapter.execute_query.call_args.args
    assert ":table_name" in sql and ":schema" in sql
    assert "$1" not in sql and "%s" not in sql
    assert params == {"table_name": "users", "schema": "public"}


@pytest.mark.asyncio
async def test_read_row_count_binds_named_params_mysql() -> None:
    adapter = FakeAdapter()
    adapter.execute_query.return_value = [{"row_count": 10}]
    reader = SampleReader(adapter, db_type="mysql", schema="db")
    assert await reader.read_row_count("users") == 10
    sql, params = adapter.execute_query.call_args.args
    assert ":table_name" in sql and ":schema" in sql and "%s" not in sql
    assert params == {"schema": "db", "table_name": "users"}
