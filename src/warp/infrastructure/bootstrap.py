"""Composition root: builds outbound adapters and wires the `Container`."""

from collections.abc import Callable

from warp.adapters.outbound.catalog_store.file_store import CatalogFileStore
from warp.adapters.outbound.db.comment_reader import CommentReader
from warp.adapters.outbound.db.factory import DatabaseFactory
from warp.adapters.outbound.db.sample_reader import SampleReader
from warp.adapters.outbound.export.registry import default_exporters
from warp.adapters.outbound.llm.providers import LLMClient
from warp.application.config import DatabaseConfig, Settings
from warp.application.container import AnalysisFactory, Container
from warp.application.ports.catalog_repository import CatalogRepository
from warp.application.ports.database import DatabaseGateway, DatabaseGatewayFactory
from warp.application.ports.text_generation import TextGenerator
from warp.application.services.catalog_analysis import CatalogAnalysisService
from warp.application.services.catalog_export import CatalogExportService
from warp.application.services.catalog_review import CatalogReviewService
from warp.application.services.cross_reference import CrossReferenceService


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
        review=CatalogReviewService(repository),
        cross_reference=cross_reference,
        db_type=db_config.type,
        database_name=db_config.name,
    )


def build_container(
    settings: Settings,
    *,
    repository: CatalogRepository | None = None,
    gateway_factory: DatabaseGatewayFactory | None = None,
    text_generator_factory: Callable[[], TextGenerator] | None = None,
    analysis_factory: AnalysisFactory | None = None,
) -> Container:
    """Wire the default adapters; any of them can be replaced (tests, embedding)."""
    repo = repository if repository is not None else build_repository(settings)
    text_generators = text_generator_factory or (lambda: LLMClient.from_config(settings))

    def default_analysis(
        gateway: DatabaseGateway, db_config: DatabaseConfig, text_generator: TextGenerator
    ) -> CatalogAnalysisService:
        return build_analysis_service(
            settings=settings,
            gateway=gateway,
            db_config=db_config,
            text_generator=text_generator,
            repository=repo,
        )

    return Container(
        settings=settings,
        repository=repo,
        review=CatalogReviewService(repo),
        export=build_export_service(),
        gateway_factory=gateway_factory or DatabaseFactory(),
        text_generator_factory=text_generators,
        analysis_factory=analysis_factory or default_analysis,
    )
