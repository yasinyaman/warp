"""Composition root: builds adapters and wires them into application services."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from warp.adapters.outbound.catalog_store.file_store import CatalogFileStore
from warp.adapters.outbound.db.comment_reader import CommentReader
from warp.adapters.outbound.db.factory import DatabaseFactory
from warp.adapters.outbound.db.sample_reader import SampleReader
from warp.adapters.outbound.export.registry import default_exporters
from warp.adapters.outbound.llm.providers import LLMClient
from warp.application.config import DatabaseConfig, Settings
from warp.application.ports.catalog_repository import CatalogRepository
from warp.application.ports.database import DatabaseGateway
from warp.application.ports.text_generation import TextGenerator
from warp.application.services.catalog_analysis import CatalogAnalysisService
from warp.application.services.catalog_export import CatalogExportService
from warp.application.services.catalog_review import CatalogReviewService
from warp.application.services.cross_reference import CrossReferenceService
from warp.application.services.pipeline import PipelineService


def build_repository(settings: Settings) -> CatalogFileStore:
    """File store configured from `settings.catalog`."""
    return CatalogFileStore(
        settings.settings.catalog.storage_path,
        default_format=settings.settings.catalog.default_format,
    )


def build_export_service() -> CatalogExportService:
    """Export service over the default exporter registry."""
    return CatalogExportService(default_exporters())


def build_analysis_service(
    *,
    settings: Settings,
    gateway: DatabaseGateway,
    db_config: DatabaseConfig,
    text_generator: TextGenerator,
    repository: CatalogRepository,
) -> CatalogAnalysisService:
    """Wire readers, review and cross-reference around a connected gateway."""
    schema = "public" if db_config.type == "postgresql" else db_config.database
    review = CatalogReviewService(repository)
    cross_reference = (
        CrossReferenceService(repository, exclude_db=db_config.name)
        if settings.settings.catalog.auto_cross_reference
        else None
    )
    return CatalogAnalysisService(
        gateway=gateway,
        config=settings,
        text_generator=text_generator,
        comments=CommentReader(
            gateway, db_type=db_config.type, schema=schema, database=db_config.name
        ),
        samples=SampleReader(gateway, db_type=db_config.type, schema=schema),
        review=review,
        cross_reference=cross_reference,
        db_type=db_config.type,
        database_name=db_config.name,
    )


@asynccontextmanager
async def open_analysis(
    settings: Settings,
    db_name: str,
    *,
    repository: CatalogRepository | None = None,
    gateway: DatabaseGateway | None = None,
) -> AsyncIterator[CatalogAnalysisService]:
    """Yield an analysis service for `db_name`, owning what it opens.

    A `gateway` passed in is reused (not closed); otherwise one is created and
    connected for the duration. The LLM client is always opened and closed here.

    Raises:
        DatabaseNotConfiguredError: If `db_name` is not configured.
    """
    db_config = settings.database(db_name)
    repo = repository or build_repository(settings)
    owns_gateway = gateway is None
    gw = gateway or DatabaseFactory.create(db_config)
    if owns_gateway:
        await gw.connect()
    try:
        text_generator = LLMClient.from_config(settings)
        try:
            yield build_analysis_service(
                settings=settings,
                gateway=gw,
                db_config=db_config,
                text_generator=text_generator,
                repository=repo,
            )
        finally:
            await text_generator.close()
    finally:
        if owns_gateway:
            await gw.disconnect()


def make_pipeline(settings: Settings) -> PipelineService:
    """Pipeline service bound to `settings`."""
    return PipelineService(
        settings=settings,
        analysis_opener=lambda name: open_analysis(settings, name),
        export=build_export_service(),
    )
