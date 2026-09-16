"""Tests for the end-to-end Pipeline orchestrator.

All heavy collaborators (DatabaseFactory, LLMClient, EnrichedAnalyzer,
OpenAPIEnricher, get_exporter) are patched so no DB/LLM/network is touched.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from warp.application.config import Settings
from warp.application.services.pipeline import Pipeline, PipelineResult
from warp.domain.catalog import DatabaseCatalog, LocalizedText, TableCatalogEntry
from warp.domain.errors import WarpError


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        databases=[
            {
                "name": "testdb",
                "type": "postgresql",
                "host": "h",
                "port": 5432,
                "database": "testdb",
                "username": "u",
                "password": "p",
            }
        ],
        settings={"catalog": {"storage_path": str(tmp_path / "catalogs")}},
    )


def _catalog() -> DatabaseCatalog:
    return DatabaseCatalog(
        database_name="testdb",
        tables={
            "users": TableCatalogEntry(
                table_name="users",
                description=LocalizedText(texts={"en": "Users"}),
            )
        },
    )


def test_pipeline_result_defaults() -> None:
    cat = _catalog()
    result = PipelineResult(catalog=cat)
    assert result.catalog is cat
    assert result.enriched_openapi_path is None
    assert result.export_path is None
    assert result.export_content is None


@pytest.mark.asyncio
async def test_pipeline_db_config_not_found(tmp_path: Path) -> None:
    pipeline = Pipeline(_settings(tmp_path))
    with pytest.raises(WarpError, match="config not found"):
        await pipeline.run(database_name="missing")


@pytest.mark.asyncio
async def test_pipeline_run_basic(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    catalog = _catalog()

    fake_adapter = AsyncMock()
    fake_analyzer = MagicMock()
    fake_analyzer.analyze = AsyncMock(return_value=catalog)
    fake_llm = AsyncMock()

    with (
        patch(
            "warp.application.services.pipeline.DatabaseFactory.create", return_value=fake_adapter
        ),
        patch("warp.application.services.pipeline.LLMClient.from_config", return_value=fake_llm),
        patch("warp.application.services.pipeline.EnrichedAnalyzer", return_value=fake_analyzer),
    ):
        result = await pipeline_run(settings)

    assert result.catalog is catalog
    fake_adapter.connect.assert_awaited_once()
    fake_adapter.disconnect.assert_awaited_once()
    fake_llm.close.assert_awaited_once()


async def pipeline_run(settings: Settings) -> PipelineResult:
    pipeline = Pipeline(settings)
    return await pipeline.run(database_name="testdb")


@pytest.mark.asyncio
async def test_pipeline_run_with_export_path(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    catalog = _catalog()
    out = tmp_path / "out.json"

    fake_adapter = AsyncMock()
    fake_analyzer = MagicMock()
    fake_analyzer.analyze = AsyncMock(return_value=catalog)
    fake_exporter = MagicMock()
    fake_exporter.export.return_value = out

    with (
        patch(
            "warp.application.services.pipeline.DatabaseFactory.create", return_value=fake_adapter
        ),
        patch("warp.application.services.pipeline.LLMClient.from_config", return_value=AsyncMock()),
        patch("warp.application.services.pipeline.EnrichedAnalyzer", return_value=fake_analyzer),
        patch("warp.application.services.pipeline.get_exporter", return_value=fake_exporter),
    ):
        pipeline = Pipeline(settings)
        result = await pipeline.run(
            database_name="testdb", export_format="json", export_path=str(out)
        )

    assert result.export_path == str(out)
    fake_exporter.export.assert_called_once()


@pytest.mark.asyncio
async def test_pipeline_run_with_export_content(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    catalog = _catalog()

    fake_analyzer = MagicMock()
    fake_analyzer.analyze = AsyncMock(return_value=catalog)
    fake_exporter = MagicMock()
    fake_exporter.export_string.return_value = "rendered"

    with (
        patch(
            "warp.application.services.pipeline.DatabaseFactory.create", return_value=AsyncMock()
        ),
        patch("warp.application.services.pipeline.LLMClient.from_config", return_value=AsyncMock()),
        patch("warp.application.services.pipeline.EnrichedAnalyzer", return_value=fake_analyzer),
        patch("warp.application.services.pipeline.get_exporter", return_value=fake_exporter),
    ):
        pipeline = Pipeline(settings)
        result = await pipeline.run(database_name="testdb", export_format="yaml")

    assert result.export_content == "rendered"
    assert result.export_path is None


@pytest.mark.asyncio
async def test_pipeline_run_with_openapi(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    catalog = _catalog()
    enriched_out = tmp_path / "enriched.json"

    fake_analyzer = MagicMock()
    fake_analyzer.analyze = AsyncMock(return_value=catalog)
    fake_enricher = MagicMock()
    fake_enricher.enrich_file.return_value = enriched_out

    with (
        patch(
            "warp.application.services.pipeline.DatabaseFactory.create", return_value=AsyncMock()
        ),
        patch("warp.application.services.pipeline.LLMClient.from_config", return_value=AsyncMock()),
        patch("warp.application.services.pipeline.EnrichedAnalyzer", return_value=fake_analyzer),
        patch("warp.application.services.pipeline.OpenAPIEnricher", return_value=fake_enricher),
    ):
        pipeline = Pipeline(settings)
        result = await pipeline.run(database_name="testdb", openapi_spec_path="spec.json")

    assert result.enriched_openapi_path == str(enriched_out)
    fake_enricher.enrich_file.assert_called_once_with("spec.json")
