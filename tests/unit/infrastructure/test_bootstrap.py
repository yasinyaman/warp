"""Tests for the composition root wiring."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from warp.adapters.outbound.catalog_store.file_store import CatalogFileStore
from warp.adapters.outbound.db.comment_reader import CommentReader
from warp.adapters.outbound.db.sample_reader import SampleReader
from warp.application.config import DatabaseConfig, Settings
from warp.application.services.catalog_review import CatalogReviewService
from warp.application.services.cross_reference import CrossReferenceService
from warp.domain.errors import DatabaseNotConfiguredError
from warp.infrastructure import bootstrap


def _settings(tmp_path, cross_ref=True) -> Settings:
    return Settings(
        databases=[DatabaseConfig(name="testdb", type="postgresql", database="d", username="u")],
        settings={
            "catalog": {
                "storage_path": str(tmp_path / "catalogs"),
                "default_format": "yaml",
                "auto_cross_reference": cross_ref,
            },
            "llm": {"provider": "ollama", "model": "m"},
        },
    )


def test_repository_uses_configured_format(tmp_path):
    repo = bootstrap.build_repository(_settings(tmp_path))
    assert isinstance(repo, CatalogFileStore)
    assert repo.default_format == "yaml"


def test_analysis_service_is_fully_wired(tmp_path):
    settings = _settings(tmp_path)
    service = bootstrap.build_analysis_service(
        settings=settings,
        gateway=MagicMock(),
        db_config=settings.database("testdb"),
        text_generator=MagicMock(),
        repository=bootstrap.build_repository(settings),
    )
    assert isinstance(service.comments, CommentReader)
    assert isinstance(service.samples, SampleReader)
    assert isinstance(service.review, CatalogReviewService)
    assert isinstance(service.cross_reference, CrossReferenceService)
    assert service.cross_reference.exclude_db == "testdb"
    assert service.database_name == "testdb"

    no_xref = bootstrap.build_analysis_service(
        settings=_settings(tmp_path, cross_ref=False),
        gateway=MagicMock(),
        db_config=settings.database("testdb"),
        text_generator=MagicMock(),
        repository=bootstrap.build_repository(settings),
    )
    assert no_xref.cross_reference is None


@pytest.mark.asyncio
async def test_open_analysis_owns_gateway_and_text_generator(tmp_path):
    settings = _settings(tmp_path)
    gateway = AsyncMock()
    text_generator = AsyncMock()
    with (
        patch.object(bootstrap.DatabaseFactory, "create", return_value=gateway) as create,
        patch.object(bootstrap.LLMClient, "from_config", return_value=text_generator),
    ):
        async with bootstrap.open_analysis(settings, "testdb") as analysis:
            assert analysis.gateway is gateway
            gateway.connect.assert_awaited_once()
    create.assert_called_once_with(settings.database("testdb"))  # full DatabaseConfig
    gateway.disconnect.assert_awaited_once()
    text_generator.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_open_analysis_reuses_given_gateway(tmp_path):
    settings = _settings(tmp_path)
    gateway = AsyncMock()
    with patch.object(bootstrap.LLMClient, "from_config", return_value=AsyncMock()):
        async with bootstrap.open_analysis(settings, "testdb", gateway=gateway):
            pass
    gateway.connect.assert_not_awaited()
    gateway.disconnect.assert_not_awaited()


@pytest.mark.asyncio
async def test_open_analysis_unknown_database(tmp_path):
    with pytest.raises(DatabaseNotConfiguredError):
        async with bootstrap.open_analysis(_settings(tmp_path), "nope"):
            pass


def test_make_pipeline(tmp_path):
    pipeline = bootstrap.make_pipeline(_settings(tmp_path))
    assert "json" in pipeline.export.formats
