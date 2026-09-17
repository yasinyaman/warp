"""Tests for the composition root and the Container it builds."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from warp.adapters.outbound.catalog_store.file_store import CatalogFileStore
from warp.adapters.outbound.db.comment_reader import CommentReader
from warp.adapters.outbound.db.factory import DatabaseFactory
from warp.adapters.outbound.db.sample_reader import SampleReader
from warp.application.config import DatabaseConfig, Settings
from warp.application.container import Container
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


class _Factory:
    def __init__(self, gateway):
        self.gateway = gateway
        self.configs = []

    def create(self, config):
        self.configs.append(config)
        return self.gateway

    def get_supported_types(self):
        return ["postgresql"]


def test_repository_uses_configured_format(tmp_path):
    repo = bootstrap.build_repository(_settings(tmp_path))
    assert isinstance(repo, CatalogFileStore)
    assert repo.default_format == "yaml"


def test_build_container_defaults(tmp_path):
    container = bootstrap.build_container(_settings(tmp_path))
    assert isinstance(container, Container)
    assert isinstance(container.repository, CatalogFileStore)
    assert isinstance(container.review, CatalogReviewService)
    assert container.review.repository is container.repository
    assert isinstance(container.gateway_factory, DatabaseFactory)
    assert "markdown" in container.export.formats


def test_analysis_service_is_fully_wired(tmp_path):
    settings = _settings(tmp_path)
    container = bootstrap.build_container(settings)
    service = container.analysis_factory(MagicMock(), settings.database("testdb"), MagicMock())
    assert isinstance(service.comments, CommentReader)
    assert isinstance(service.samples, SampleReader)
    assert isinstance(service.review, CatalogReviewService)
    assert service.review.repository is container.repository
    assert isinstance(service.cross_reference, CrossReferenceService)
    assert service.cross_reference.exclude_db == "testdb"
    assert service.database_name == "testdb"

    no_xref = bootstrap.build_container(_settings(tmp_path, cross_ref=False))
    assert (
        no_xref.analysis_factory(
            MagicMock(), settings.database("testdb"), MagicMock()
        ).cross_reference
        is None
    )


@pytest.mark.asyncio
async def test_open_analysis_owns_gateway_and_text_generator(tmp_path):
    settings = _settings(tmp_path)
    gateway, text_generator = AsyncMock(), AsyncMock()
    factory = _Factory(gateway)
    container = bootstrap.build_container(
        settings, gateway_factory=factory, text_generator_factory=lambda: text_generator
    )
    async with container.open_analysis("testdb") as analysis:
        assert analysis.gateway is gateway
        gateway.connect.assert_awaited_once()
    assert factory.configs == [settings.database("testdb")]  # the full DatabaseConfig
    gateway.disconnect.assert_awaited_once()
    text_generator.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_open_analysis_reuses_given_gateway(tmp_path):
    gateway = AsyncMock()
    container = bootstrap.build_container(_settings(tmp_path), text_generator_factory=AsyncMock)
    async with container.open_analysis("testdb", gateway=gateway):
        pass
    gateway.connect.assert_not_awaited()
    gateway.disconnect.assert_not_awaited()


@pytest.mark.asyncio
async def test_open_analysis_unknown_database(tmp_path):
    container = bootstrap.build_container(_settings(tmp_path))
    with pytest.raises(DatabaseNotConfiguredError):
        async with container.open_analysis("nope"):
            pass


@pytest.mark.asyncio
async def test_default_text_generator_comes_from_llm_client(tmp_path):
    settings = _settings(tmp_path)
    container = bootstrap.build_container(settings)
    with patch.object(bootstrap.LLMClient, "from_config", return_value=AsyncMock()) as from_config:
        container.text_generator_factory()
    from_config.assert_called_once_with(settings)


def test_pipeline_is_bound_to_the_container(tmp_path):
    container = bootstrap.build_container(_settings(tmp_path))
    pipeline = container.pipeline()
    assert pipeline.export is container.export
    assert pipeline.settings is container.settings
