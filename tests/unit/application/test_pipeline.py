"""Tests for PipelineService with an injected analysis opener and export service."""

from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from warp.application.config import Settings
from warp.application.services.catalog_export import CatalogExportService
from warp.application.services.pipeline import PipelineResult, PipelineService
from warp.domain.catalog import DatabaseCatalog, LocalizedText, TableCatalogEntry
from warp.domain.errors import DatabaseNotConfiguredError, UnsupportedExportFormatError


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
                table_name="users", description=LocalizedText(texts={"en": "Users"})
            )
        },
    )


class _Opener:
    """Records how the analysis context is used."""

    def __init__(self, catalog: DatabaseCatalog) -> None:
        self.catalog = catalog
        self.names: list[str] = []
        self.entered = 0
        self.exited = 0

    def __call__(self, name: str):
        self.names.append(name)

        @asynccontextmanager
        async def cm():
            self.entered += 1
            analysis = MagicMock()
            analysis.analyze = AsyncMock(return_value=self.catalog)
            try:
                yield analysis
            finally:
                self.exited += 1

        return cm()


def _pipeline(
    tmp_path: Path, opener: _Opener, exporter: MagicMock | None = None
) -> PipelineService:
    exporters = {"json": exporter or MagicMock(), "yaml": exporter or MagicMock()}
    return PipelineService(
        settings=_settings(tmp_path), analysis_opener=opener, export=CatalogExportService(exporters)
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
    opener = _Opener(_catalog())
    with pytest.raises(DatabaseNotConfiguredError, match="config not found"):
        await _pipeline(tmp_path, opener).run(database_name="missing")
    assert opener.entered == 0


@pytest.mark.asyncio
async def test_pipeline_run_basic(tmp_path: Path) -> None:
    catalog = _catalog()
    opener = _Opener(catalog)
    result = await _pipeline(tmp_path, opener).run(database_name="testdb")
    assert result.catalog is catalog
    assert opener.names == ["testdb"]
    assert (opener.entered, opener.exited) == (1, 1)  # analysis context closed


@pytest.mark.asyncio
async def test_pipeline_run_with_export_path(tmp_path: Path) -> None:
    out = tmp_path / "out.json"
    exporter = MagicMock()
    exporter.export.return_value = out
    result = await _pipeline(tmp_path, _Opener(_catalog()), exporter).run(
        database_name="testdb", export_format="json", export_path=str(out)
    )
    assert result.export_path == str(out)
    exporter.export.assert_called_once()


@pytest.mark.asyncio
async def test_pipeline_run_with_export_content(tmp_path: Path) -> None:
    exporter = MagicMock()
    exporter.export_string.return_value = "rendered"
    result = await _pipeline(tmp_path, _Opener(_catalog()), exporter).run(
        database_name="testdb", export_format="yaml"
    )
    assert result.export_content == "rendered"
    assert result.export_path is None


@pytest.mark.asyncio
async def test_pipeline_unknown_export_format(tmp_path: Path) -> None:
    with pytest.raises(UnsupportedExportFormatError):
        await _pipeline(tmp_path, _Opener(_catalog())).run(
            database_name="testdb", export_format="xml"
        )


@pytest.mark.asyncio
async def test_pipeline_run_with_openapi(tmp_path: Path) -> None:
    enriched_out = tmp_path / "enriched.json"
    fake_enricher = MagicMock()
    fake_enricher.enrich_file.return_value = enriched_out
    with patch("warp.application.services.pipeline.OpenAPIEnricher", return_value=fake_enricher):
        result = await _pipeline(tmp_path, _Opener(_catalog())).run(
            database_name="testdb", openapi_spec_path="spec.json"
        )
    assert result.enriched_openapi_path == str(enriched_out)
    fake_enricher.enrich_file.assert_called_once_with("spec.json")
