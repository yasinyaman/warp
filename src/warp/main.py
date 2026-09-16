"""Warp Engine - Main Application Entry Point.

Automatically generates REST CRUD endpoints from database schema.
Production-ready with connection retry, proper error handling, and logging.
"""

import asyncio
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

from warp import __version__
from warp.api.auth import AuthManager, Permission, init_auth_manager
from warp.api.catalog_router import _refresh_openapi_enrichment, create_catalog_router
from warp.api.query import create_query_router
from warp.api.router_factory import RouterFactory
from warp.catalog.store import CatalogFileStore
from warp.config.settings import Settings, load_config, validate_production_config
from warp.core.exceptions import AutoCrudException, ConfigurationError, DatabaseConnectionError
from warp.core.logging import get_logger, setup_logging
from warp.database.base import DatabaseAdapter
from warp.database.factory import DatabaseFactory
from warp.schema.analyzer import SchemaAnalyzer
from warp.schema.models import DatabaseSchema

# Environment configuration
APP_ENV = os.getenv("APP_ENV", "development")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FORMAT = os.getenv("LOG_FORMAT", "json" if APP_ENV == "production" else "colored")
CORS_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()]

# Setup logging
setup_logging(level=LOG_LEVEL, json_format=(LOG_FORMAT == "json"))
logger = get_logger(__name__)


# Application state
class AppState:
    """Container for application state."""

    settings: Settings | None = None
    auth_manager: AuthManager | None = None
    databases: dict[str, DatabaseAdapter] = {}
    readonly_databases: dict[str, DatabaseAdapter] = {}
    schemas: dict[str, DatabaseSchema] = {}
    is_ready: bool = False


state = AppState()


async def connect_with_retry(
    adapter: DatabaseAdapter, max_retries: int = 5, retry_delay: float = 2.0
) -> None:
    """Connect to database with exponential backoff retry.

    Args:
        adapter: Database adapter to connect.
        max_retries: Maximum number of retry attempts.
        retry_delay: Initial delay between retries (doubles each attempt).
    """
    last_error = None

    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"Connection attempt {attempt}/{max_retries} to {adapter.name}")
            await adapter.connect()
            logger.info(f"Successfully connected to {adapter.name}")
            return
        except Exception as e:
            last_error = e
            if attempt < max_retries:
                wait_time = retry_delay * (2 ** (attempt - 1))
                logger.warning(
                    f"Failed to connect to {adapter.name}: {e}. Retrying in {wait_time:.1f}s..."
                )
                await asyncio.sleep(wait_time)
            else:
                logger.error(f"All connection attempts to {adapter.name} failed")

    raise DatabaseConnectionError(
        f"Failed to connect to {adapter.name} after {max_retries} attempts",
        details={"last_error": str(last_error)},
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:  # noqa: C901, PLR0912, PLR0915
    """Application lifespan handler.

    Handles startup (database connection, schema discovery) and
    shutdown (cleanup) events.
    """
    logger.info(f"Starting Warp Engine v{__version__} (env={APP_ENV})...")

    # Load configuration
    try:
        config_path = os.getenv("CONFIG_PATH")
        state.settings = load_config(config_path)
        logger.info(f"Loaded configuration with {len(state.settings.databases)} database(s)")
    except FileNotFoundError as e:
        logger.error(f"Configuration error: {e}")
        raise

    # Fail-safe: refuse to start in production with unsafe configuration.
    violations = validate_production_config(state.settings, APP_ENV, CORS_ORIGINS)
    if violations:
        for violation in violations:
            logger.error(f"Unsafe production configuration: {violation}")
        raise ConfigurationError(
            "Refusing to start in production with unsafe configuration",
            details={"violations": violations},
        )

    # Initialize auth manager
    auth_manager = init_auth_manager(state.settings.settings.auth)
    state.auth_manager = auth_manager
    if auth_manager.enabled:
        logger.info(f"Authentication enabled with {auth_manager.api_key_count} API key(s)")
    else:
        logger.info("Authentication disabled - all endpoints are public")

    # Connect to databases and discover schemas
    for db_config in state.settings.databases:
        db_name = db_config.name
        logger.info(f"Connecting to database: {db_name} ({db_config.type})")

        try:
            # Create adapter
            adapter = DatabaseFactory.create(db_config.model_dump())

            # Connect with retry mechanism
            await connect_with_retry(adapter)
            state.databases[db_name] = adapter

            # Analyze schema
            if state.settings.settings.auto_discover_tables:
                analyzer = SchemaAnalyzer(
                    adapter, excluded_tables=state.settings.settings.excluded_tables
                )
                schema = await analyzer.analyze()
                state.schemas[db_name] = schema

                table_names = schema.get_table_names()
                logger.info(
                    f"Discovered {len(schema.tables)} tables in {db_name}: "
                    f"{', '.join(table_names[:10])}{'...' if len(table_names) > 10 else ''}"
                )

                # Create routers for all tables
                # Use db_name when multiple databases are configured
                use_db_name = db_name if len(state.settings.databases) > 1 else None

                router_factory = RouterFactory(
                    db=adapter,
                    schema_analyzer=analyzer,
                    default_limit=state.settings.settings.pagination.default_limit,
                    max_limit=state.settings.settings.pagination.max_limit,
                    db_name=use_db_name,
                    auth_manager=auth_manager,
                    readonly_columns=state.settings.settings.readonly_columns,
                )

                routers = router_factory.create_routers_for_all_tables(schema.tables)

                # Use db_name prefix when multiple databases are configured
                db_prefix = f"/{db_name}" if len(state.settings.databases) > 1 else ""

                for router in routers:
                    app.include_router(
                        router, prefix=f"{state.settings.settings.api_prefix}{db_prefix}"
                    )

                # Raw query endpoint: use a separate read-only connection when
                # configured, so a whitelist bypass still cannot mutate data.
                query_adapter = adapter
                readonly_config = db_config.readonly_config()
                if readonly_config and state.settings.settings.enable_raw_query:
                    readonly_adapter = DatabaseFactory.create(readonly_config)
                    await connect_with_retry(readonly_adapter)
                    state.readonly_databases[db_name] = readonly_adapter
                    query_adapter = readonly_adapter
                    logger.info(f"Raw query endpoint for {db_name} uses a read-only connection")

                # Add raw query router
                query_router = create_query_router(
                    db=query_adapter,
                    whitelist=state.settings.settings.raw_query_whitelist,
                    enabled=state.settings.settings.enable_raw_query,
                    auth_manager=auth_manager,
                )
                app.include_router(
                    query_router,
                    prefix=f"{state.settings.settings.api_prefix}{db_prefix}",
                    tags=[f"{db_name} - Raw Query"]
                    if len(state.settings.databases) > 1
                    else ["Raw Query"],
                )

        except DatabaseConnectionError:
            raise
        except Exception as e:
            logger.error(f"Failed to setup database {db_name}: {e}")
            raise

    # Register catalog router
    catalog_store = None
    try:
        catalog_store = CatalogFileStore(state.settings.settings.catalog.storage_path)
        catalog_router = create_catalog_router(
            store=catalog_store,
            config=state.settings,
            adapters=state.databases,
            app=app,
            auth_manager=auth_manager,
        )
        app.include_router(
            catalog_router,
            prefix=state.settings.settings.api_prefix,
        )
        logger.info("Catalog API router registered")
    except Exception as e:
        logger.warning(f"Failed to initialize catalog router: {e}")

    # Auto-enrich OpenAPI spec with catalog descriptions at startup
    if catalog_store and state.databases:
        try:
            _refresh_openapi_enrichment(app, catalog_store, state.settings, state.databases)
        except Exception as e:
            logger.warning(f"Failed to setup OpenAPI auto-enrichment: {e}")

    state.is_ready = True
    logger.info("Warp Engine started successfully!")
    logger.info(f"API documentation available at: {state.settings.settings.docs_url}")

    yield

    # Shutdown: disconnect from databases
    logger.info("Shutting down Warp Engine...")
    state.is_ready = False

    for db_name, adapter in state.databases.items():
        try:
            await adapter.disconnect()
            logger.info(f"Disconnected from {db_name}")
        except Exception as e:
            logger.error(f"Error disconnecting from {db_name}: {e}")

    for db_name, adapter in state.readonly_databases.items():
        try:
            await adapter.disconnect()
            logger.info(f"Disconnected read-only connection for {db_name}")
        except Exception as e:
            logger.error(f"Error disconnecting read-only {db_name}: {e}")


def create_app() -> FastAPI:  # noqa: C901
    """Create and configure the FastAPI application.

    Returns:
        Configured FastAPI application instance.
    """
    # Determine docs URL based on environment
    docs_url = "/docs" if APP_ENV != "production" else None

    app = FastAPI(
        title="Warp Engine",
        description="""
# Warp - Auto-discovery REST CRUD API Generator

Automatically generates REST API endpoints from database schema.

## Features

- **Auto-discovery**: Automatically discovers database tables and generates endpoints
- **CRUD Operations**: Full Create, Read, Update, Delete support
- **Filtering**: Filter results using query parameters
- **Sorting**: Sort results by any column
- **Pagination**: Built-in pagination support
- **Raw Queries**: Execute raw SQL queries (when enabled)
- **Multi-DB Support**: PostgreSQL and MySQL support

## Filtering Examples

```
GET /api/v1/users?filter[status]=active
GET /api/v1/products?filter[price][gte]=100&filter[price][lte]=500
GET /api/v1/orders?filter[status][in]=pending,processing
```

## Sorting Examples

```
GET /api/v1/users?sort=name:asc
GET /api/v1/products?sort=price:desc,name:asc
GET /api/v1/orders?sort=-created_at
```

## Pagination

```
GET /api/v1/users?limit=20&offset=40
```
        """,
        version=__version__,
        # Disable default docs — we serve custom /docs with cache-busting
        docs_url=None,
        redoc_url=None,
        # The spec route is registered explicitly below so it can be guarded
        # by the auth manager (it carries catalog descriptions/x-llm-context).
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.get("/openapi.json", include_in_schema=False)
    async def openapi_json(request: Request) -> JSONResponse:
        """Serve the (possibly catalog-enriched) OpenAPI spec.

        Public only when "/openapi.json" is listed in `auth.public_paths`
        (the development default); otherwise a key with `read` permission is
        required, like any other endpoint.
        """
        manager = state.auth_manager
        if manager and manager.enabled:
            user = await manager.get_current_user(request)
            if user is not None and not user.has_permission(Permission.READ):
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Permission denied: 'read' required"},
                )
        return JSONResponse(app.openapi())

    # Custom /docs and /redoc with cache-busting for OpenAPI JSON
    if docs_url:
        from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html

        @app.get("/docs", include_in_schema=False)
        async def custom_swagger_ui() -> HTMLResponse:
            return get_swagger_ui_html(
                openapi_url=f"/openapi.json?v={int(time.time())}",
                title=f"{app.title} - Swagger UI",
            )

        @app.get("/redoc", include_in_schema=False)
        async def custom_redoc() -> HTMLResponse:
            return get_redoc_html(
                openapi_url=f"/openapi.json?v={int(time.time())}",
                title=f"{app.title} - ReDoc",
            )

    # CORS middleware.
    # Browsers reject a "*" allowlist combined with credentials, so credentials
    # are only enabled when an explicit origin allowlist is configured.
    allow_credentials = "*" not in CORS_ORIGINS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_credentials=allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Global exception handler
    @app.exception_handler(AutoCrudException)
    async def auto_crud_exception_handler(request: Request, exc: AutoCrudException) -> JSONResponse:
        logger.error(f"Application error: {exc.message}")
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict())

    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception(f"Unhandled error: {exc}")
        return JSONResponse(
            status_code=500,
            content={
                "error": "InternalServerError",
                "message": "An unexpected error occurred",
                "details": {} if APP_ENV == "production" else {"error": str(exc)},
            },
        )

    # Health check endpoint (Kubernetes/Docker compatible)
    # response_model=None: the handler returns dict | JSONResponse (a union that
    # FastAPI cannot build a response model from).
    @app.get("/health", tags=["Health"], response_model=None)
    async def health_check() -> dict[str, Any] | JSONResponse:
        """Health check endpoint for container orchestration.

        Returns:
            - 200: Service is healthy
            - 503: Service is unhealthy (database disconnected)
        """
        db_status = {}
        all_healthy = True

        for name, adapter in state.databases.items():
            is_connected = adapter.is_connected
            db_status[name] = "connected" if is_connected else "disconnected"
            if not is_connected:
                all_healthy = False

        status = "healthy" if all_healthy and state.is_ready else "unhealthy"

        response = {
            "status": status,
            "ready": state.is_ready,
            "databases": db_status,
            "environment": APP_ENV,
        }

        if not all_healthy:
            return JSONResponse(status_code=503, content=response)

        return response

    # Readiness probe (Kubernetes)
    @app.get("/ready", tags=["Health"], response_model=None)
    async def readiness_check() -> dict[str, Any] | JSONResponse:
        """Readiness probe for Kubernetes.

        Returns 200 only when the application is fully ready to serve traffic.
        """
        if not state.is_ready:
            return JSONResponse(
                status_code=503, content={"ready": False, "message": "Application not ready"}
            )

        return {"ready": True}

    # Liveness probe (Kubernetes)
    @app.get("/live", tags=["Health"])
    async def liveness_check() -> dict[str, Any]:
        """Liveness probe for Kubernetes.

        Returns 200 if the application process is alive.
        """
        return {"alive": True}

    # Info endpoint
    @app.get("/info", tags=["Info"])
    async def api_info() -> dict[str, Any]:
        """Get API information and discovered tables."""
        tables_info = {}
        for db_name, schema in state.schemas.items():
            tables_info[db_name] = {
                "tables": schema.get_table_names(),
                "table_count": len(schema.tables),
            }

        # Catalog info
        catalog_info = {}
        if state.settings:
            try:
                _store = CatalogFileStore(state.settings.settings.catalog.storage_path)
                catalog_names = _store.list_catalogs()
                catalog_info = {
                    "available_catalogs": catalog_names,
                    "catalog_count": len(catalog_names),
                    "storage_path": state.settings.settings.catalog.storage_path,
                }
            except Exception:
                catalog_info = {"available_catalogs": [], "catalog_count": 0}

        return {
            "name": "Warp Engine",
            "version": __version__,
            "environment": APP_ENV,
            "databases": tables_info,
            "catalog": catalog_info,
            "settings": {
                "api_prefix": state.settings.settings.api_prefix if state.settings else "/api/v1",
                "pagination": {
                    "default_limit": state.settings.settings.pagination.default_limit
                    if state.settings
                    else 50,
                    "max_limit": state.settings.settings.pagination.max_limit
                    if state.settings
                    else 1000,
                },
                "raw_query_enabled": state.settings.settings.enable_raw_query
                if state.settings
                else False,
            },
        }

    return app


# Create application instance
app = create_app()


def main() -> None:
    """Entry point for the application."""
    import uvicorn

    host = os.getenv("API_HOST", "0.0.0.0")
    port = int(os.getenv("API_PORT", "8000"))
    workers = int(os.getenv("API_WORKERS", "1"))
    reload = APP_ENV == "development"

    uvicorn.run(
        "warp.main:app",
        host=host,
        port=port,
        workers=workers if not reload else 1,
        reload=reload,
        access_log=APP_ENV != "production",
    )


if __name__ == "__main__":
    main()
