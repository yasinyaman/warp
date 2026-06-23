"""Catalog REST API endpoints.

Provides REST endpoints for catalog operations:
- Analyze database and generate catalog
- List available catalogs
- Get catalog info
- Export catalog
- Enrich OpenAPI spec
"""

from typing import Any

from fastapi import APIRouter, BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from warp.catalog.store import CatalogFileStore
from warp.core.logging import get_logger
from warp.export.markdown_exporter import get_exporter
from warp.integration.openapi_enricher import OpenAPIEnricher

logger = get_logger(__name__)


# --- Request/Response models ---


class AnalyzeRequest(BaseModel):
    """Request body for catalog analysis."""
    database: str = Field(..., description="Database config name")
    tables: list[str] | None = Field(default=None, description="Specific tables to analyze")
    lang: str | None = Field(default=None, description="Language override")
    format: str = Field(default="json", description="Storage format: json or yaml")
    auto_approve: bool = Field(default=False, description="Auto-approve catalog after analysis (skip review)")


class AnalyzeResponse(BaseModel):
    """Response from catalog analysis."""
    database: str
    table_count: int
    languages: list[str]
    status: str = "draft"


class CatalogListEntry(BaseModel):
    """Summary of a catalog in the list."""
    database_name: str
    database_type: str = "postgresql"
    table_count: int = 0
    languages: list[str] = Field(default_factory=list)
    version: str = "1.0.0"
    status: str = "draft"


class CatalogListResponse(BaseModel):
    """Response for catalog list."""
    catalogs: list[CatalogListEntry]
    total: int


class CatalogInfoResponse(BaseModel):
    """Detailed catalog info."""
    database_name: str
    database_type: str
    table_count: int
    languages: list[str]
    tables: list[dict[str, Any]]
    llm_provider: str | None = None
    llm_model: str | None = None


class ExportRequest(BaseModel):
    """Request for catalog export."""
    format: str = Field(default="json", description="Export format: json, yaml, markdown")
    lang: str = Field(default="en", description="Language for export")


class TableEditRequest(BaseModel):
    """Partial update for a table's editable fields."""
    description: dict[str, str] | None = Field(
        default=None,
        description="Localized description updates, e.g. {'en': 'New desc'}",
    )
    human_name: dict[str, str] | None = Field(
        default=None,
        description="Localized human name updates",
    )
    tags: list[str] | None = Field(
        default=None,
        description="Replace tags list",
    )
    relationships: list[dict[str, Any]] | None = Field(
        default=None,
        description="Replace relationships list",
    )


class ColumnEditRequest(BaseModel):
    """Partial update for a column's editable fields."""
    description: dict[str, str] | None = Field(
        default=None,
        description="Localized description updates",
    )
    semantic_type: str | None = Field(
        default=None,
        description="Semantic type override",
    )
    tags: list[str] | None = Field(
        default=None,
        description="Replace tags list",
    )


class ApproveRequest(BaseModel):
    """Request to approve tables or entire catalog."""
    tables: list[str] | None = Field(
        default=None,
        description="Specific table names to approve. If None, approve all.",
    )


class DraftInfoResponse(BaseModel):
    """Draft catalog info with per-table review status."""
    database_name: str
    database_type: str
    status: str
    table_count: int
    languages: list[str]
    review_summary: dict[str, int]
    tables: list[dict[str, Any]]


class TableReviewDetail(BaseModel):
    """Detailed table info for review."""
    table_name: str
    human_name: dict[str, str]
    description: dict[str, str]
    review_status: str
    tags: list[str]
    column_count: int
    row_count: int | None = None
    columns: list[dict[str, Any]]
    relationships: list[dict[str, Any]]
    user_overrides: dict[str, Any] = Field(default_factory=dict)


def _refresh_openapi_enrichment(
    app: FastAPI,
    store: CatalogFileStore,
    config: Any,
    adapters: dict[str, Any],
) -> None:
    """Rebuild OpenAPI enrichment after catalog changes.

    Clears the cached OpenAPI schema and sets up a new enriched_openapi
    function using all available catalogs.
    """
    if not config or not config.settings.catalog.auto_enrich_openapi:
        logger.debug("OpenAPI enrichment disabled in config")
        return

    app.openapi_schema = None  # Clear cache

    enrichment_lang = config.settings.catalog.openapi_enrichment_lang
    enrichers = []

    for db_name in adapters:
        catalog = store.load(db_name)
        if catalog:
            logger.debug(
                f"OpenAPI enrichment: loaded catalog '{db_name}' "
                f"(status={catalog.status.value}, "
                f"tables={list(catalog.tables.keys())})"
            )
            enrichers.append(OpenAPIEnricher(catalog, lang=enrichment_lang))
        else:
            logger.debug(f"OpenAPI enrichment: no catalog found for '{db_name}'")

    if enrichers:
        from fastapi.openapi.utils import get_openapi

        def enriched_openapi() -> dict[str, Any]:
            if app.openapi_schema:
                return app.openapi_schema

            logger.debug("Generating enriched OpenAPI schema...")

            schema = get_openapi(
                title=app.title,
                version=app.version,
                description=app.description,
                routes=app.routes,
            )

            for enricher in enrichers:
                schema = enricher.enrich(schema)

            app.openapi_schema = schema
            logger.debug("Enriched OpenAPI schema cached")
            return app.openapi_schema

        app.openapi = enriched_openapi  # type: ignore[method-assign]
        # Force regeneration: call it now so the next /openapi.json returns enriched
        enriched_openapi()
        logger.info(f"OpenAPI enrichment refreshed with {len(enrichers)} catalog(s)")
    else:
        app.openapi_schema = None


def create_catalog_router(  # noqa: C901, PLR0915
    store: CatalogFileStore,
    config: Any = None,
    adapters: dict[str, Any] | None = None,
    app: FastAPI | None = None,
) -> APIRouter:
    """Create the catalog API router.

    Args:
        store: CatalogFileStore instance
        config: Settings instance (for analysis)
        adapters: Dict of connected database adapters (for live analysis)
        app: FastAPI app instance (for OpenAPI enrichment refresh)

    Returns:
        FastAPI APIRouter with catalog endpoints
    """
    router = APIRouter(prefix="/catalog", tags=["Catalog"])

    @router.get(
        "",
        response_model=CatalogListResponse,
        summary="List available catalogs",
    )
    async def list_catalogs() -> CatalogListResponse:
        """List all available catalogs."""
        catalog_names = store.list_catalogs()
        index = store.get_index()

        entries = []
        for name in catalog_names:
            idx_entry = index.catalogs.get(name)
            if idx_entry:
                entries.append(CatalogListEntry(
                    database_name=idx_entry.database_name,
                    database_type=idx_entry.database_type,
                    table_count=idx_entry.table_count,
                    languages=idx_entry.languages,
                    version=idx_entry.version,
                    status=getattr(idx_entry, "status", "draft"),
                ))
            else:
                entries.append(CatalogListEntry(database_name=name))

        return CatalogListResponse(catalogs=entries, total=len(entries))

    @router.get(
        "/{db_name}",
        response_model=CatalogInfoResponse,
        summary="Get catalog info",
    )
    async def get_catalog_info(
        db_name: str,
        lang: str = Query(default="en", description="Language for descriptions"),
    ) -> CatalogInfoResponse:
        """Get detailed information about a specific catalog."""
        catalog = store.load(db_name)
        if not catalog:
            raise HTTPException(status_code=404, detail=f"Catalog not found: {db_name}")

        tables_info = []
        for tname, table in catalog.tables.items():
            human = table.human_name.get(lang) or tname
            desc = table.description.get(lang) or ""
            tables_info.append({
                "table_name": tname,
                "human_name": human,
                "description": desc,
                "column_count": len(table.columns),
                "row_count": table.row_count,
                "tags": table.tags,
            })

        return CatalogInfoResponse(
            database_name=catalog.database_name,
            database_type=catalog.database_type,
            table_count=catalog.table_count,
            languages=catalog.languages,
            tables=tables_info,
            llm_provider=catalog.llm_provider,
            llm_model=catalog.llm_model,
        )

    @router.get(
        "/{db_name}/export",
        summary="Export catalog",
    )
    async def export_catalog(
        db_name: str,
        format: str = Query(default="json", description="Export format"),
        lang: str = Query(default="en", description="Language"),
    ) -> Response:
        """Export a catalog in the specified format."""
        catalog = store.load(db_name)
        if not catalog:
            raise HTTPException(status_code=404, detail=f"Catalog not found: {db_name}")

        try:
            exporter = get_exporter(format)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

        content = exporter.export_string(catalog, lang=lang)

        content_types = {
            "json": "application/json",
            "yaml": "text/yaml",
            "markdown": "text/markdown",
            "md": "text/markdown",
        }

        return Response(
            content=content,
            media_type=content_types.get(format, "text/plain"),
            headers={
                "Content-Disposition": f'attachment; filename="{db_name}_catalog.{format}"'
            },
        )

    @router.post(
        "/analyze",
        response_model=AnalyzeResponse,
        summary="Analyze database and generate catalog",
    )
    async def analyze_database(
        request: AnalyzeRequest,
        background_tasks: BackgroundTasks,
    ) -> AnalyzeResponse:
        """Trigger database analysis and catalog generation.

        This starts the analysis process. For large databases,
        the analysis runs as a background task.
        """
        if not config or not adapters:
            raise HTTPException(
                status_code=503,
                detail="Analysis not available - config or adapters not initialized",
            )

        if request.database not in adapters:
            raise HTTPException(
                status_code=404,
                detail=f"Database not connected: {request.database}",
            )

        # Run analysis
        from warp.enrichment.analyzer import EnrichedAnalyzer
        from warp.llm.client import LLMClient

        adapter = adapters[request.database]
        db_config = None
        for db in config.databases:
            if db.name == request.database:
                db_config = db
                break

        if not db_config:
            raise HTTPException(
                status_code=404,
                detail=f"Database config not found: {request.database}",
            )

        from warp.core.exceptions import AnalysisError, LLMError

        try:
            llm_client = LLMClient.from_config(config)
            try:
                analyzer = EnrichedAnalyzer(
                    adapter=adapter,
                    config=config,
                    llm_client=llm_client,
                    store=store,
                    db_type=db_config.type,
                    schema="public" if db_config.type == "postgresql" else db_config.database,
                    database_name=request.database,
                )

                catalog = await analyzer.analyze(
                    table_names=request.tables,
                    auto_approve=request.auto_approve,
                )

                # Refresh OpenAPI docs if catalog was auto-approved
                if catalog.status.value == "approved" and app and adapters:
                    _refresh_openapi_enrichment(app, store, config, adapters)

                return AnalyzeResponse(
                    database=catalog.database_name,
                    table_count=catalog.table_count,
                    languages=catalog.languages,
                    status=catalog.status.value,
                )
            finally:
                await llm_client.close()
        except (AnalysisError, LLMError) as e:
            logger.error(f"Analysis failed for {request.database}: {e}")
            raise HTTPException(
                status_code=422,
                detail=str(e),
            ) from e
        except Exception as e:
            logger.error(f"Analysis failed for {request.database}: {e}")
            raise HTTPException(status_code=500, detail=f"Analysis failed: {e}") from e

    @router.delete(
        "/{db_name}",
        summary="Delete a catalog",
    )
    async def delete_catalog(db_name: str) -> dict[str, Any]:
        """Delete a stored catalog."""
        if store.delete(db_name):
            return {"deleted": True, "database": db_name}
        raise HTTPException(status_code=404, detail=f"Catalog not found: {db_name}")

    # --- Draft / Review endpoints ---

    @router.get(
        "/{db_name}/draft",
        response_model=DraftInfoResponse,
        summary="Get draft catalog with review status",
    )
    async def get_draft_info(
        db_name: str,
        lang: str = Query(default="en", description="Language for descriptions"),
    ) -> DraftInfoResponse:
        """Get draft catalog overview with per-table review status."""
        catalog = store.load(db_name)
        if not catalog:
            raise HTTPException(status_code=404, detail=f"Catalog not found: {db_name}")

        tables_info = []
        for tname, table in catalog.tables.items():
            tables_info.append({
                "table_name": tname,
                "human_name": table.human_name.get(lang) or tname,
                "description": table.description.get(lang) or "",
                "review_status": table.review_status.value,
                "column_count": len(table.columns),
                "row_count": table.row_count,
                "tags": table.tags,
                "has_overrides": bool(table.user_overrides),
            })

        return DraftInfoResponse(
            database_name=catalog.database_name,
            database_type=catalog.database_type,
            status=catalog.status.value,
            table_count=catalog.table_count,
            languages=catalog.languages,
            review_summary=catalog.review_summary,
            tables=tables_info,
        )

    @router.get(
        "/{db_name}/draft/tables/{table_name}",
        response_model=TableReviewDetail,
        summary="Get single table detail for review",
    )
    async def get_draft_table(
        db_name: str,
        table_name: str,
        lang: str = Query(default="en", description="Language"),
    ) -> TableReviewDetail:
        """Get detailed table info for review including columns and relationships."""
        catalog = store.load(db_name)
        if not catalog:
            raise HTTPException(status_code=404, detail=f"Catalog not found: {db_name}")

        table = catalog.get_table(table_name)
        if not table:
            raise HTTPException(
                status_code=404,
                detail=f"Table not found: {table_name} in {db_name}",
            )

        columns_info = []
        for col in table.columns:
            columns_info.append({
                "name": col.name,
                "data_type": col.data_type,
                "description": col.description.texts,
                "semantic_type": col.semantic_type,
                "nullable": col.nullable,
                "is_primary_key": col.is_primary_key,
                "is_foreign_key": col.is_foreign_key,
                "references": col.references,
                "tags": col.tags,
                "sample_values": col.sample_values,
                "has_overrides": bool(col.user_overrides),
            })

        relationships_info = []
        for rel in table.relationships:
            relationships_info.append({
                "source_column": rel.source_column,
                "target_table": rel.target_table,
                "target_column": rel.target_column,
                "relationship_type": rel.relationship_type,
                "description": rel.description.texts,
            })

        return TableReviewDetail(
            table_name=table.table_name,
            human_name=table.human_name.texts,
            description=table.description.texts,
            review_status=table.review_status.value,
            tags=table.tags,
            column_count=len(table.columns),
            row_count=table.row_count,
            columns=columns_info,
            relationships=relationships_info,
            user_overrides=table.user_overrides,
        )

    @router.patch(
        "/{db_name}/draft/tables/{table_name}",
        summary="Edit table fields in draft",
    )
    async def edit_draft_table(
        db_name: str,
        table_name: str,
        request: TableEditRequest,
        lang: str = Query(default="en", description="Default language for string values"),
    ) -> dict[str, Any]:
        """Edit table-level fields. The table review_status becomes 'modified'."""
        catalog = store.load(db_name)
        if not catalog:
            raise HTTPException(status_code=404, detail=f"Catalog not found: {db_name}")

        if catalog.status.value != "draft":
            raise HTTPException(
                status_code=409,
                detail=f"Catalog is not in draft status: {db_name}",
            )

        updates = request.model_dump(exclude_none=True)
        if not updates:
            raise HTTPException(status_code=400, detail="No fields to update")

        try:
            table = store.update_table_fields(
                db_name=db_name,
                table_name=table_name,
                updates=updates,
                lang=lang,
            )
            return {
                "table_name": table.table_name,
                "review_status": table.review_status.value,
                "updated_fields": list(updates.keys()),
            }
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @router.patch(
        "/{db_name}/draft/tables/{table_name}/columns/{col_name}",
        summary="Edit column fields in draft",
    )
    async def edit_draft_column(
        db_name: str,
        table_name: str,
        col_name: str,
        request: ColumnEditRequest,
        lang: str = Query(default="en", description="Default language"),
    ) -> dict[str, Any]:
        """Edit column-level fields. The parent table review_status becomes 'modified'."""
        catalog = store.load(db_name)
        if not catalog:
            raise HTTPException(status_code=404, detail=f"Catalog not found: {db_name}")

        if catalog.status.value != "draft":
            raise HTTPException(
                status_code=409,
                detail=f"Catalog is not in draft status: {db_name}",
            )

        updates = request.model_dump(exclude_none=True)
        if not updates:
            raise HTTPException(status_code=400, detail="No fields to update")

        try:
            column = store.update_column_fields(
                db_name=db_name,
                table_name=table_name,
                column_name=col_name,
                updates=updates,
                lang=lang,
            )
            return {
                "column_name": column.name,
                "semantic_type": column.semantic_type,
                "updated_fields": list(updates.keys()),
            }
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @router.post(
        "/{db_name}/approve",
        summary="Approve catalog (all or specific tables)",
    )
    async def approve_catalog_endpoint(
        db_name: str,
        request: ApproveRequest = ApproveRequest(),
    ) -> dict[str, Any]:
        """Approve all tables or specific tables in a draft catalog.

        If request.tables is None, approves all pending tables and
        transitions the catalog to 'approved' status.
        """
        catalog = store.load(db_name)
        if not catalog:
            raise HTTPException(status_code=404, detail=f"Catalog not found: {db_name}")

        if request.tables:
            for tname in request.tables:
                try:
                    store.approve_table(db_name, tname)
                except Exception as e:
                    raise HTTPException(status_code=400, detail=str(e)) from e

            catalog = store.load_or_raise(db_name)
            if catalog.all_tables_approved:
                catalog = store.approve_catalog(db_name)
        else:
            catalog = store.approve_catalog(db_name)

        # Refresh OpenAPI enrichment after approval
        if catalog.status.value == "approved" and app and adapters:
            _refresh_openapi_enrichment(app, store, config, adapters)

        return {
            "database": db_name,
            "status": catalog.status.value,
            "review_summary": catalog.review_summary,
        }

    @router.post(
        "/{db_name}/approve/{table_name}",
        summary="Approve a single table",
    )
    async def approve_single_table(
        db_name: str,
        table_name: str,
    ) -> dict[str, Any]:
        """Approve a single table in the draft catalog."""
        try:
            catalog = store.approve_table(db_name, table_name)
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

        if catalog.all_tables_approved:
            catalog = store.approve_catalog(db_name)
            if app and adapters:
                _refresh_openapi_enrichment(app, store, config, adapters)

        return {
            "database": db_name,
            "table": table_name,
            "catalog_status": catalog.status.value,
            "review_summary": catalog.review_summary,
        }

    return router
