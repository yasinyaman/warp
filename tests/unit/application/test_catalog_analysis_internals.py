"""Tests for CatalogAnalysisService internal helpers and flows.

Covers privacy controls (_samples_for_llm), entry builders, conversion helpers,
the translation flow, and an end-to-end analyze() with sample data and
cross-references. The adapter and LLM client are mocked - no DB/LLM/network.
"""

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from warp.adapters.outbound.catalog_store.file_store import CatalogFileStore
from warp.adapters.outbound.db.comment_reader import CommentReader
from warp.adapters.outbound.db.sample_reader import SampleReader
from warp.application.config import Settings
from warp.application.services.catalog_analysis import CatalogAnalysisService
from warp.application.services.catalog_review import CatalogReviewService
from warp.domain.samples import ColumnStats, TableSamples


class FakeAdapter:
    def __init__(self) -> None:
        self.get_tables = AsyncMock(return_value=["users"])
        self.get_table_schema = AsyncMock(
            return_value={
                "columns": [
                    {"name": "id", "type": "integer", "nullable": False, "key": "PRI"},
                    {"name": "email", "type": "varchar", "nullable": True},
                ],
                "foreign_keys": [
                    {
                        "column": "org_id",
                        "references_table": "orgs",
                        "references_column": "id",
                        "constraint_name": "fk",
                    }
                ],
                "indexes": [{"name": "ix", "columns": ["email"], "unique": False}],
                "primary_key": "id",
            }
        )
        self.execute_query = AsyncMock(return_value=[])


def _settings(
    tmp_path: Path,
    provider: str = "ollama",
    share_cloud: bool = False,
    mask: bool = True,
    sample_limit: int = 5,
    strategy: str = "single",
    languages: list[str] | None = None,
) -> Settings:
    return Settings(
        settings={
            "catalog": {
                "storage_path": str(tmp_path / "catalogs"),
                "auto_cross_reference": False,
            },
            "analysis": {
                "sample_limit": sample_limit,
                "share_samples_with_cloud_llm": share_cloud,
                "mask_pii_samples": mask,
            },
            "llm": {"provider": provider, "model": "m", "api_key": "k"},
            "i18n": {
                "languages": languages or ["en"],
                "default_language": "en",
                "translation_strategy": strategy,
            },
        }
    )


def _analyzer(tmp_path: Path, settings: Settings, llm: Any = None) -> CatalogAnalysisService:
    adapter = FakeAdapter()
    return CatalogAnalysisService(
        gateway=adapter,
        config=settings,
        text_generator=llm or AsyncMock(),
        comments=CommentReader(adapter, db_type="postgresql", schema="public", database="testdb"),
        samples=SampleReader(adapter, db_type="postgresql", schema="public"),
        review=CatalogReviewService(CatalogFileStore(tmp_path / "catalogs")),
        db_type="postgresql",
        database_name="testdb",
    )


def _samples() -> TableSamples:
    return TableSamples(
        table_name="users",
        row_count=10,
        column_samples={"email": ["a@x.com"], "id": [1]},
        column_stats={"email": ColumnStats(column_name="email", sample_values=["a@x.com"])},
    )


# --- _samples_for_llm privacy controls ---


def test_samples_for_llm_none(tmp_path: Path) -> None:
    analyzer = _analyzer(tmp_path, _settings(tmp_path))
    assert analyzer._samples_for_llm(None) is None


def test_samples_for_llm_cloud_blocked(tmp_path: Path) -> None:
    analyzer = _analyzer(tmp_path, _settings(tmp_path, provider="openai", share_cloud=False))
    assert analyzer._samples_for_llm(_samples()) is None


def test_samples_for_llm_cloud_allowed_masks(tmp_path: Path) -> None:
    analyzer = _analyzer(
        tmp_path, _settings(tmp_path, provider="openai", share_cloud=True, mask=True)
    )
    out = analyzer._samples_for_llm(_samples())
    assert out is not None
    assert out.column_samples["email"] == ["***"]


def test_samples_for_llm_local_masks(tmp_path: Path) -> None:
    analyzer = _analyzer(tmp_path, _settings(tmp_path, provider="ollama", mask=True))
    out = analyzer._samples_for_llm(_samples())
    assert out is not None
    assert out.column_samples["email"] == ["***"]


def test_samples_for_llm_no_mask(tmp_path: Path) -> None:
    analyzer = _analyzer(tmp_path, _settings(tmp_path, provider="ollama", mask=False))
    out = analyzer._samples_for_llm(_samples())
    assert out is not None
    assert out.column_samples["email"] == ["a@x.com"]


# --- conversion helpers ---


def test_columns_to_dicts_variants() -> None:
    class WithModelDump:
        def model_dump(self) -> dict[str, Any]:
            return {"name": "c", "type": "int"}

    class WithDict:
        def __init__(self) -> None:
            self.name = "d"
            self.type = "text"

    result = CatalogAnalysisService._columns_to_dicts(
        [{"name": "a", "type": "int"}, WithModelDump(), WithDict()]
    )
    assert result[0]["name"] == "a"
    assert result[1]["name"] == "c"
    assert result[2]["name"] == "d"


def test_fks_and_indexes_to_dicts() -> None:
    class WithModelDump:
        def model_dump(self) -> dict[str, Any]:
            return {"column": "x"}

    fks = CatalogAnalysisService._fks_to_dicts([{"column": "a"}, WithModelDump()])
    assert len(fks) == 2
    idx = CatalogAnalysisService._indexes_to_dicts([{"name": "i"}, WithModelDump()])
    assert len(idx) == 2


def test_parse_localized() -> None:
    assert CatalogAnalysisService._parse_localized({"en": "x"}).get("en") == "x"
    assert CatalogAnalysisService._parse_localized("plain").get("en") == "plain"
    assert CatalogAnalysisService._parse_localized(123).texts == {}


# --- entry builders ---


def test_build_basic_entry(tmp_path: Path) -> None:
    analyzer = _analyzer(tmp_path, _settings(tmp_path))
    entry = analyzer._build_basic_entry(
        "users",
        [{"name": "id", "type": "int"}, {"name": "email", "type": "varchar"}],
        [{"column": "org_id", "references_table": "orgs", "references_column": "id"}],
        [{"name": "ix", "columns": ["email"], "unique": False}],
        "id",
        _samples(),
        "table comment",
        {"email": "the email"},
    )
    assert entry.table_name == "users"
    assert entry.description.get("en") == "table comment"
    assert entry.row_count == 10
    email_col = entry.get_column("email")
    assert email_col is not None
    assert email_col.description.get("en") == "the email"


def test_build_enriched_entry(tmp_path: Path) -> None:
    analyzer = _analyzer(tmp_path, _settings(tmp_path))
    result = {
        "table_description": {"en": "All users"},
        "table_human_name": {"en": "Users"},
        "table_tags": ["core"],
        "columns": {
            "id": {"description": {"en": "PK"}, "semantic_type": "id", "tags": ["key"]},
            "email": "the email as string",  # string form handled
        },
        "relationships": [
            {
                "source_column": "org_id",
                "target_table": "orgs",
                "target_column": "id",
                "relationship_type": "many-to-one",
                "description": {"en": "belongs to"},
            },
            "not a dict",  # skipped
        ],
    }
    entry = analyzer._build_enriched_entry(
        table_name="users",
        result=result,
        col_dicts=[
            {"name": "id", "type": "integer", "key": "PRI"},
            {"name": "email", "type": "varchar"},
            {"name": "org_id", "type": "integer"},
        ],
        fk_dicts=[{"column": "org_id", "references_table": "orgs", "references_column": "id"}],
        idx_dicts=[],
        primary_key="id",
        samples=_samples(),
        db_table_comment=None,
        db_column_comments={},
    )
    assert entry.human_name.get("en") == "Users"
    assert entry.tags == ["core"]
    id_col = entry.get_column("id")
    assert id_col is not None and id_col.is_primary_key is True
    org_col = entry.get_column("org_id")
    assert org_col is not None and org_col.is_foreign_key is True
    assert org_col.references == "orgs.id"
    email_col = entry.get_column("email")
    assert email_col is not None
    assert email_col.description.get("en") == "the email as string"
    assert len(entry.relationships) == 1


def test_build_enriched_entry_columns_as_list(tmp_path: Path) -> None:
    analyzer = _analyzer(tmp_path, _settings(tmp_path))
    result = {
        "columns": [{"name": "id", "description": {"en": "pk"}, "semantic_type": "id"}],
    }
    entry = analyzer._build_enriched_entry(
        table_name="t",
        result=result,
        col_dicts=[{"name": "id", "type": "int"}],
        fk_dicts=[],
        idx_dicts=[],
        primary_key=["id"],  # composite list form
        samples=None,
        db_table_comment=None,
        db_column_comments={},
    )
    col = entry.get_column("id")
    assert col is not None and col.is_primary_key is True


def test_build_enriched_entry_not_dict_raises(tmp_path: Path) -> None:
    analyzer = _analyzer(tmp_path, _settings(tmp_path))
    with pytest.raises(ValueError):
        analyzer._build_enriched_entry(
            table_name="t",
            result="not a dict",  # type: ignore[arg-type]
            col_dicts=[],
            fk_dicts=[],
            idx_dicts=[],
            primary_key=None,
            samples=None,
            db_table_comment=None,
            db_column_comments={},
        )


# --- translation ---


@pytest.mark.asyncio
async def test_translate_text_empty(tmp_path: Path) -> None:
    analyzer = _analyzer(tmp_path, _settings(tmp_path))
    assert await analyzer._translate_text("   ", "en", "tr") is None


@pytest.mark.asyncio
async def test_translate_text_error(tmp_path: Path) -> None:
    llm = AsyncMock()
    llm.generate = AsyncMock(side_effect=RuntimeError("boom"))
    analyzer = _analyzer(tmp_path, _settings(tmp_path), llm=llm)
    assert await analyzer._translate_text("hi", "en", "tr") is None


@pytest.mark.asyncio
async def test_translate_catalog(tmp_path: Path) -> None:
    from warp.domain.catalog import (
        ColumnCatalogEntry,
        DatabaseCatalog,
        LocalizedText,
        TableCatalogEntry,
    )

    settings = _settings(tmp_path, strategy="multi", languages=["en", "tr"])
    llm = AsyncMock()
    llm.generate = AsyncMock(return_value="translated")
    analyzer = _analyzer(tmp_path, settings, llm=llm)

    catalog = DatabaseCatalog(
        database_name="t",
        tables={
            "users": TableCatalogEntry(
                table_name="users",
                description=LocalizedText(texts={"en": "Users desc"}),
                human_name=LocalizedText(texts={"en": "Users"}),
                columns=[
                    ColumnCatalogEntry(
                        name="id",
                        data_type="int",
                        description=LocalizedText(texts={"en": "PK"}),
                    )
                ],
            )
        },
        languages=["en", "tr"],
    )
    out = await analyzer._translate_catalog(catalog)
    table = out.get_table("users")
    assert table is not None
    assert table.description.get("tr") == "translated"
    assert table.human_name.get("tr") == "translated"
    assert table.columns[0].description.get("tr") == "translated"


# --- end-to-end analyze() ---


@pytest.mark.asyncio
async def test_analyze_end_to_end_with_llm(tmp_path: Path) -> None:
    settings = _settings(tmp_path, provider="ollama")
    llm = AsyncMock()
    llm.generate_json = AsyncMock(
        return_value=json.dumps(
            {
                "table_description": {"en": "Users table"},
                "table_human_name": {"en": "Users"},
                "table_tags": ["core"],
                "columns": {
                    "id": {"description": {"en": "PK"}, "semantic_type": "id"},
                    "email": {"description": {"en": "email"}, "semantic_type": "email"},
                },
                "relationships": [],
            }
        )
    )
    analyzer = _analyzer(tmp_path, settings, llm=llm)
    catalog = await analyzer.analyze()
    assert catalog.table_count == 1
    users = catalog.get_table("users")
    assert users is not None
    assert users.human_name.get("en") == "Users"
    # saved as draft
    assert catalog.status.value == "draft"
    assert (tmp_path / "catalogs" / "testdb").exists()


@pytest.mark.asyncio
async def test_analyze_auto_approve(tmp_path: Path) -> None:
    settings = _settings(tmp_path, provider="ollama")
    llm = AsyncMock()
    llm.generate_json = AsyncMock(
        return_value=json.dumps(
            {"table_description": {"en": "x"}, "columns": {}, "relationships": []}
        )
    )
    analyzer = _analyzer(tmp_path, settings, llm=llm)
    catalog = await analyzer.analyze(auto_approve=True)
    assert catalog.status.value == "approved"


@pytest.mark.asyncio
async def test_analyze_all_llm_failure_raises(tmp_path: Path) -> None:
    from warp.domain.errors import AnalysisError

    settings = _settings(tmp_path, provider="ollama")
    llm = AsyncMock()
    llm.generate_json = AsyncMock(return_value="not valid json")
    analyzer = _analyzer(tmp_path, settings, llm=llm)
    # All tables fail LLM with zero successes -> AnalysisError.
    with pytest.raises(AnalysisError):
        await analyzer.analyze()


@pytest.mark.asyncio
async def test_analyze_partial_llm_failure_fallback(tmp_path: Path) -> None:
    settings = _settings(tmp_path, provider="ollama")
    analyzer = _analyzer(tmp_path, settings)
    # Two tables: first returns valid JSON, second is invalid -> fallback entry.
    analyzer.gateway.get_tables = AsyncMock(return_value=["users", "orders"])
    good = json.dumps({"table_description": {"en": "x"}, "columns": {}, "relationships": []})
    llm = AsyncMock()
    llm.generate_json = AsyncMock(side_effect=[good, "not valid json"])
    analyzer.text_generator = llm
    catalog = await analyzer.analyze()
    # Both tables present; one enriched, one fallback - no error raised.
    assert catalog.table_count == 2


@pytest.mark.asyncio
async def test_analyze_requested_table_not_found(tmp_path: Path) -> None:
    from warp.domain.errors import AnalysisError

    settings = _settings(tmp_path, provider="ollama")
    analyzer = _analyzer(tmp_path, settings)
    with pytest.raises(AnalysisError):
        await analyzer.analyze(table_names=["ghost_table"])


@pytest.mark.asyncio
async def test_analyze_resolves_case(tmp_path: Path) -> None:
    settings = _settings(tmp_path, provider="ollama")
    llm = AsyncMock()
    llm.generate_json = AsyncMock(
        return_value=json.dumps(
            {"table_description": {"en": "x"}, "columns": {}, "relationships": []}
        )
    )
    analyzer = _analyzer(tmp_path, settings, llm=llm)
    # adapter exposes "users"; request "Users" -> resolved
    catalog = await analyzer.analyze(table_names=["Users"])
    assert catalog.get_table("users") is not None


@pytest.mark.asyncio
async def test_report_lists_failed_and_fallback_tables(tmp_path: Path) -> None:
    """A table whose schema cannot be read is dropped and reported; one whose LLM
    output is unusable is kept schema-only and reported as a fallback."""
    good = json.dumps({"table_description": {"en": "Users"}, "columns": {}})
    llm = AsyncMock()
    llm.generate_json = AsyncMock(side_effect=[good, "not json {{{"])
    analyzer = _analyzer(tmp_path, _settings(tmp_path), llm)
    analyzer.gateway.get_tables = AsyncMock(return_value=["users", "orders", "broken"])
    original = analyzer.gateway.get_table_schema

    async def schema(table: str):
        if table == "broken":
            raise RuntimeError("permission denied")
        return await original(table)

    analyzer.gateway.get_table_schema = schema

    catalog = await analyzer.analyze()
    report = analyzer.report
    assert report is not None and report.catalog is catalog
    assert set(catalog.tables) == {"users", "orders"}
    assert report.failed_tables == ["broken"]
    assert report.fallback_tables == ["orders"]
    assert report.llm_successes == 1
    assert report.has_problems
