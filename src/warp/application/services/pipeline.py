"""End-to-end pipeline: DB -> catalog -> optional OpenAPI enrichment / export."""

import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from warp.application.config import Settings
from warp.application.services.catalog_analysis import AnalysisReport, CatalogAnalysisService
from warp.application.services.catalog_export import CatalogExportService
from warp.application.services.openapi_enrichment import OpenAPIEnricher
from warp.domain.catalog import DatabaseCatalog

logger = logging.getLogger(__name__)

AnalysisOpener = Callable[[str], AbstractAsyncContextManager[CatalogAnalysisService]]


class PipelineResult:
    """Result from a pipeline run."""

    def __init__(self, catalog: DatabaseCatalog, report: AnalysisReport | None = None):
        """Store the generated catalog (and analysis report) and initialize result fields."""
        self.catalog = catalog
        self.report = report
        self.enriched_openapi_path: str | None = None
        self.export_path: str | None = None
        self.export_content: str | None = None


class PipelineService:
    """Runs analysis for one configured database, then enrichment/export."""

    def __init__(
        self,
        *,
        settings: Settings,
        analysis_opener: AnalysisOpener,
        export: CatalogExportService,
    ):
        """`analysis_opener(db_name)` yields a ready-to-use analysis service."""
        self.settings = settings
        self._open_analysis = analysis_opener
        self.export = export

    async def run(
        self,
        database_name: str,
        lang: str | None = None,
        table_names: list[str] | None = None,
        export_format: str | None = None,
        export_path: str | None = None,
        openapi_spec_path: str | None = None,
    ) -> PipelineResult:
        """Run the full pipeline.

        Raises:
            DatabaseNotConfiguredError: If `database_name` is not configured.
        """
        lang = lang or self.settings.settings.i18n.default_language
        self.settings.database(database_name)  # fail fast on an unknown name
        logger.info(f"Pipeline started: {database_name} (lang={lang})")

        async with self._open_analysis(database_name) as analysis:
            catalog = await analysis.analyze(table_names=table_names)
            report = analysis.report

        result = PipelineResult(catalog=catalog, report=report)

        if openapi_spec_path:
            enricher = OpenAPIEnricher(
                catalog,
                lang=lang,
                include_examples=self.settings.settings.catalog.openapi_include_examples,
            )
            enriched_path = enricher.enrich_file(openapi_spec_path)
            result.enriched_openapi_path = str(enriched_path)
            logger.info(f"OpenAPI spec enriched: {enriched_path}")

        if export_format:
            if export_path:
                result.export_path = str(
                    self.export.export(catalog, export_format, export_path, lang=lang)
                )
            else:
                result.export_content = self.export.render(catalog, export_format, lang=lang)

        logger.info(f"Pipeline completed: {database_name} ({catalog.table_count} tables)")
        return result
