"""The application's wired object graph (built by `infrastructure.bootstrap`).

Inbound adapters (HTTP, CLI) receive a `Container` and never construct
outbound adapters themselves; everything they need is a port or a service.
"""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from warp.application.config import DatabaseConfig, Settings
from warp.application.ports.catalog_repository import CatalogRepository
from warp.application.ports.database import DatabaseGateway, DatabaseGatewayFactory
from warp.application.ports.text_generation import TextGenerator
from warp.application.services.catalog_analysis import CatalogAnalysisService
from warp.application.services.catalog_export import CatalogExportService
from warp.application.services.catalog_review import CatalogReviewService
from warp.application.services.pipeline import PipelineService

AnalysisFactory = Callable[[DatabaseGateway, DatabaseConfig, TextGenerator], CatalogAnalysisService]


@dataclass(frozen=True)
class Container:
    """Services and port implementations for one configuration."""

    settings: Settings
    repository: CatalogRepository
    review: CatalogReviewService
    export: CatalogExportService
    gateway_factory: DatabaseGatewayFactory
    text_generator_factory: Callable[[], TextGenerator]
    analysis_factory: AnalysisFactory

    def database(self, name: str) -> DatabaseConfig:
        """Configuration of the database called `name` (404-style error if unknown)."""
        return self.settings.database(name)

    @asynccontextmanager
    async def open_gateway(self, db_config: DatabaseConfig) -> AsyncIterator[DatabaseGateway]:
        """Create, connect and finally disconnect a gateway for `db_config`."""
        gateway = self.gateway_factory.create(db_config)
        await gateway.connect()
        try:
            yield gateway
        finally:
            await gateway.disconnect()

    @asynccontextmanager
    async def open_analysis(
        self, db_name: str, *, gateway: DatabaseGateway | None = None
    ) -> AsyncIterator[CatalogAnalysisService]:
        """Yield an analysis service for `db_name`.

        A `gateway` passed in is reused (and not closed); otherwise one is
        opened for the duration. The text generator is always created and
        closed here.

        Raises:
            DatabaseNotConfiguredError: If `db_name` is not configured.
        """
        db_config = self.database(db_name)
        if gateway is None:
            async with (
                self.open_gateway(db_config) as owned,
                self._analysis(owned, db_config) as analysis,
            ):
                yield analysis
        else:
            async with self._analysis(gateway, db_config) as analysis:
                yield analysis

    @asynccontextmanager
    async def _analysis(
        self, gateway: DatabaseGateway, db_config: DatabaseConfig
    ) -> AsyncIterator[CatalogAnalysisService]:
        text_generator = self.text_generator_factory()
        try:
            yield self.analysis_factory(gateway, db_config, text_generator)
        finally:
            await text_generator.close()

    def pipeline(self) -> PipelineService:
        """Pipeline service bound to this container."""
        return PipelineService(
            settings=self.settings,
            analysis_opener=self.open_analysis,
            export=self.export,
        )
