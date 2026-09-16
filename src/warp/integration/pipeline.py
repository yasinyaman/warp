"""End-to-end pipeline: DB -> Catalog -> Enriched MCP.

Orchestrates the full flow from database connection
to enriched MCP server ready for LLM consumption.
"""

from typing import Any

from warp.catalog.models import DatabaseCatalog
from warp.catalog.store import CatalogFileStore
from warp.config.settings import Settings
from warp.core.exceptions import AutoCrudException
from warp.core.logging import get_logger
from warp.database.factory import DatabaseFactory
from warp.enrichment.analyzer import EnrichedAnalyzer
from warp.export.markdown_exporter import get_exporter
from warp.integration.openapi_enricher import OpenAPIEnricher
from warp.llm.client import LLMClient

logger = get_logger(__name__)


class Pipeline:
    """End-to-end pipeline for database catalog generation and enrichment."""

    def __init__(self, config: Settings):
        """Store config and open the catalog store."""
        self.config = config
        self.store = CatalogFileStore(
            config.settings.catalog.storage_path,
            default_format=config.settings.catalog.default_format,
        )

    async def run(
        self,
        database_name: str,
        lang: str | None = None,
        table_names: list[str] | None = None,
        export_format: str | None = None,
        export_path: str | None = None,
        openapi_spec_path: str | None = None,
    ) -> "PipelineResult":
        """Run the full pipeline."""
        lang = lang or self.config.settings.i18n.default_language

        logger.info(f"Pipeline started: {database_name} (lang={lang})")

        # Get database config
        db_config = None
        for db in self.config.databases:
            if db.name == database_name:
                db_config = db
                break

        if not db_config:
            raise AutoCrudException(f"Database config not found: {database_name}")

        # Create adapter
        config_dict = {
            "type": db_config.type,
            "host": db_config.host,
            "port": db_config.port,
            "database": db_config.database,
            "username": db_config.username,
            "password": db_config.password,
            **db_config.options,
        }

        adapter = DatabaseFactory.create(config_dict)
        await adapter.connect()

        try:
            llm_client = LLMClient.from_config(self.config)

            try:
                analyzer = EnrichedAnalyzer(
                    adapter=adapter,
                    config=self.config,
                    llm_client=llm_client,
                    store=self.store,
                    db_type=db_config.type,
                    schema="public" if db_config.type == "postgresql" else db_config.database,
                    database_name=database_name,
                )

                catalog = await analyzer.analyze(table_names=table_names)

            finally:
                await llm_client.close()

            result = PipelineResult(catalog=catalog)

            # Enrich OpenAPI spec if provided
            if openapi_spec_path:
                enricher = OpenAPIEnricher(
                    catalog,
                    lang=lang,
                    include_examples=self.config.settings.catalog.openapi_include_examples,
                )
                enriched_path = enricher.enrich_file(openapi_spec_path)
                result.enriched_openapi_path = str(enriched_path)
                logger.info(f"OpenAPI spec enriched: {enriched_path}")

            # Export if requested
            if export_format:
                exporter = get_exporter(export_format)
                if export_path:
                    exp_path = exporter.export(catalog, export_path, lang=lang)
                    result.export_path = str(exp_path)
                else:
                    result.export_content = exporter.export_string(catalog, lang=lang)

            logger.info(f"Pipeline completed: {database_name} ({catalog.table_count} tables)")

            return result

        finally:
            await adapter.disconnect()


class PipelineResult:
    """Result from a pipeline run."""

    def __init__(self, catalog: DatabaseCatalog):
        """Store the generated catalog and initialize result fields."""
        self.catalog = catalog
        self.enriched_openapi_path: str | None = None
        self.enriched_mcp_server: Any = None
        self.export_path: str | None = None
        self.export_content: str | None = None
