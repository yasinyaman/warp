"""FastAPI application factory and lifespan (inbound HTTP adapter).

`create_app()` takes a *container factory* so that this module never builds
outbound adapters itself; `warp.main` supplies the factory from the
composition root.
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.types import Lifespan

from warp import __version__
from warp.adapters.inbound.http.arrow_export import arrow_available
from warp.adapters.inbound.http.auth import AuthManager, Permission
from warp.adapters.inbound.http.capabilities import capabilities_of
from warp.adapters.inbound.http.routes.catalog import (
    _refresh_openapi_enrichment,
    create_catalog_router,
)
from warp.adapters.inbound.http.routes.crud import RouterFactory
from warp.adapters.inbound.http.routes.export import create_export_router
from warp.adapters.inbound.http.routes.query import create_query_router
from warp.adapters.inbound.http.routes.schema import create_schema_router
from warp.application.config import RuntimeEnv, Settings, validate_production_config
from warp.application.container import Container
from warp.application.ports.database import DatabaseGateway
from warp.application.services.schema_discovery import SchemaAnalyzer
from warp.domain.errors import ConfigurationError, DatabaseConnectionError, WarpError
from warp.domain.schema import DatabaseSchema

logger = logging.getLogger(__name__)

ContainerFactory = Callable[[], Container]


@dataclass
class RuntimeContext:
    """What the lifespan built; stored on `app.state.runtime`."""

    env: RuntimeEnv
    container: Container | None = None
    settings: Settings | None = None
    auth_manager: AuthManager | None = None
    gateways: dict[str, DatabaseGateway] = field(default_factory=dict)
    readonly_gateways: dict[str, DatabaseGateway] = field(default_factory=dict)
    schemas: dict[str, DatabaseSchema] = field(default_factory=dict)
    is_ready: bool = False


def runtime_of(app: FastAPI) -> RuntimeContext:
    """The app's runtime context."""
    return app.state.runtime  # type: ignore[no-any-return]


async def connect_with_retry(
    gateway: DatabaseGateway, max_retries: int = 5, retry_delay: float = 2.0
) -> None:
    """Connect to database with exponential backoff retry.

    Args:
        gateway: Database gateway to connect.
        max_retries: Maximum number of retry attempts.
        retry_delay: Initial delay between retries (doubles each attempt).
    """
    last_error = None

    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"Connection attempt {attempt}/{max_retries} to {gateway.name}")
            await gateway.connect()
            logger.info(f"Successfully connected to {gateway.name}")
            return
        except Exception as e:
            last_error = e
            if attempt < max_retries:
                wait_time = retry_delay * (2 ** (attempt - 1))
                logger.warning(
                    f"Failed to connect to {gateway.name}: {e}. Retrying in {wait_time:.1f}s..."
                )
                await asyncio.sleep(wait_time)
            else:
                logger.error(f"All connection attempts to {gateway.name} failed")

    raise DatabaseConnectionError(
        f"Failed to connect to {gateway.name} after {max_retries} attempts",
        details={"last_error": str(last_error)},
    )


def _mount_prefixes(api_prefix: str, db_name: str, multi_db: bool) -> list[str]:
    """Where one database's routers are mounted: the documented prefix first.

    Every database answers at ``{api_prefix}/{db_name}``, so a client can
    address it by name however many databases are configured; that is the
    documented (OpenAPI-visible) location. With a single database the bare
    ``{api_prefix}`` is kept as a hidden alias for compatibility.

    The scoped prefix must be registered **first**: routers carry
    ``/{table}`` paths, so an alias route would otherwise swallow
    ``{api_prefix}/{db_name}/…`` with ``table = db_name``.
    """
    scoped = f"{api_prefix}/{db_name}"
    return [scoped] if multi_db else [scoped, api_prefix]


def _include_at_prefixes(
    app: FastAPI,
    routers: list[APIRouter],
    prefixes: list[str],
    tags: list[str | Enum] | None = None,
) -> None:
    """Include ``routers`` under each prefix; only the first prefix is in the schema."""
    for position, prefix in enumerate(prefixes):
        for router in routers:
            app.include_router(router, prefix=prefix, tags=tags, include_in_schema=position == 0)


async def _mount_database(  # noqa: PLR0913
    app: FastAPI,
    runtime: RuntimeContext,
    container: Container,
    db_config: Any,
    auth_manager: AuthManager,
    multi_db: bool,
) -> None:
    """Connect one configured database and mount its CRUD + raw-query routers."""
    settings = container.settings.settings
    db_name = db_config.name
    logger.info(f"Connecting to database: {db_name} ({db_config.type})")

    gateway = container.gateway_factory.create(db_config)
    await connect_with_retry(gateway)
    runtime.gateways[db_name] = gateway

    if not settings.auto_discover_tables:
        return

    analyzer = SchemaAnalyzer(gateway, excluded_tables=settings.excluded_tables)
    schema = await analyzer.analyze()
    runtime.schemas[db_name] = schema

    table_names = schema.get_table_names()
    logger.info(
        f"Discovered {len(schema.tables)} tables in {db_name}: "
        f"{', '.join(table_names[:10])}{'...' if len(table_names) > 10 else ''}"
    )

    prefixes = _mount_prefixes(settings.api_prefix, db_name, multi_db)
    # Schema and export routes first: GET /{table}/schema and /{table}/export
    # must win over CRUD's GET /{table}/{id}.
    _include_at_prefixes(
        app, [create_schema_router(db_name, schema, gateway, auth_manager)], prefixes
    )
    export_routers = [
        create_export_router(
            table, gateway, settings.export, auth_manager, db_name if multi_db else None
        )
        for table in schema.tables.values()
    ]
    _include_at_prefixes(app, export_routers, prefixes)
    router_factory = RouterFactory(
        db=gateway,
        schema_analyzer=analyzer,
        default_limit=settings.pagination.default_limit,
        max_limit=settings.pagination.max_limit,
        db_name=db_name if multi_db else None,
        auth_manager=auth_manager,
        readonly_columns=settings.readonly_columns,
    )
    _include_at_prefixes(app, router_factory.create_routers_for_all_tables(schema.tables), prefixes)

    # Raw query endpoint: use a separate read-only connection when configured,
    # so a whitelist bypass still cannot mutate data.
    query_gateway = gateway
    readonly_config = db_config.readonly_config()
    if readonly_config and settings.enable_raw_query:
        readonly_gateway = container.gateway_factory.create(readonly_config)
        await connect_with_retry(readonly_gateway)
        runtime.readonly_gateways[db_name] = readonly_gateway
        query_gateway = readonly_gateway
        logger.info(f"Raw query endpoint for {db_name} uses a read-only connection")

    query_router = create_query_router(
        db=query_gateway,
        whitelist=settings.raw_query_whitelist,
        enabled=settings.enable_raw_query,
        auth_manager=auth_manager,
    )
    _include_at_prefixes(
        app,
        [query_router],
        prefixes,
        tags=[f"{db_name} - Raw Query"] if multi_db else ["Raw Query"],
    )


def _make_lifespan(  # noqa: C901
    container_factory: ContainerFactory | None,
) -> Lifespan[FastAPI]:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:  # noqa: C901, PLR0912, PLR0915
        runtime = runtime_of(app)
        env = runtime.env
        logger.info(f"Starting Warp Engine v{__version__} (env={env.app_env})...")

        if container_factory is None:
            raise ConfigurationError(
                "create_app() was called without a container_factory; "
                "use warp.main:app or pass one explicitly"
            )
        container = container_factory()
        runtime.container = container
        runtime.settings = container.settings
        logger.info(f"Loaded configuration with {len(container.settings.databases)} database(s)")

        # Fail-safe: refuse to start in production with unsafe configuration.
        violations = validate_production_config(
            container.settings, env.app_env, list(env.cors_origins)
        )
        if violations:
            for violation in violations:
                logger.error(f"Unsafe production configuration: {violation}")
            raise ConfigurationError(
                "Refusing to start in production with unsafe configuration",
                details={"violations": violations},
            )

        auth_manager = AuthManager(container.settings.settings.auth)
        runtime.auth_manager = auth_manager
        if auth_manager.enabled:
            logger.info(f"Authentication enabled with {auth_manager.api_key_count} API key(s)")
        else:
            logger.info("Authentication disabled - all endpoints are public")

        multi_db = len(container.settings.databases) > 1
        for db_config in container.settings.databases:
            try:
                await _mount_database(app, runtime, container, db_config, auth_manager, multi_db)
            except DatabaseConnectionError:
                raise
            except Exception as e:
                logger.error(f"Failed to setup database {db_config.name}: {e}")
                raise

        try:
            app.include_router(
                create_catalog_router(
                    container=container,
                    gateways=runtime.gateways,
                    app=app,
                    auth_manager=auth_manager,
                ),
                prefix=container.settings.settings.api_prefix,
            )
            logger.info("Catalog API router registered")
        except Exception as e:
            logger.warning(f"Failed to initialize catalog router: {e}")

        if runtime.gateways:
            try:
                _refresh_openapi_enrichment(app, container, runtime.gateways)
            except Exception as e:
                logger.warning(f"Failed to setup OpenAPI auto-enrichment: {e}")

        runtime.is_ready = True
        logger.info("Warp Engine started successfully!")
        logger.info(f"API documentation available at: {container.settings.settings.docs_url}")

        yield

        logger.info("Shutting down Warp Engine...")
        runtime.is_ready = False
        for label, gateways in (("", runtime.gateways), ("read-only ", runtime.readonly_gateways)):
            for db_name, gateway in gateways.items():
                try:
                    await gateway.disconnect()
                    logger.info(f"Disconnected {label}connection for {db_name}")
                except Exception as e:
                    logger.error(f"Error disconnecting {label}{db_name}: {e}")

    return lifespan


def create_app(  # noqa: C901, PLR0915
    container_factory: ContainerFactory | None = None,
    env: RuntimeEnv | None = None,
) -> FastAPI:
    """Create and configure the FastAPI application.

    Args:
        container_factory: Builds the `Container` when the app starts (lifespan).
            Without it the app can be created (routes, docs, probes) but not
            started.
        env: Process environment; defaults to `RuntimeEnv.from_environ()`.

    Returns:
        Configured FastAPI application instance.
    """
    env = env or RuntimeEnv.from_environ()
    docs_url = "/docs" if not env.is_production else None

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
        lifespan=_make_lifespan(container_factory),
    )
    app.state.runtime = RuntimeContext(env=env)

    @app.get("/openapi.json", include_in_schema=False)
    async def openapi_json(request: Request) -> JSONResponse:
        """Serve the (possibly catalog-enriched) OpenAPI spec.

        Public only when "/openapi.json" is listed in `auth.public_paths`
        (the development default); otherwise a key with `read` permission is
        required, like any other endpoint.
        """
        manager = runtime_of(app).auth_manager
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
    cors_origins = list(env.cors_origins)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials="*" not in cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(WarpError)
    async def warp_error_handler(request: Request, exc: WarpError) -> JSONResponse:
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
                "details": {} if env.is_production else {"error": str(exc)},
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
        runtime = runtime_of(app)
        db_status = {}
        all_healthy = True

        for name, gateway in runtime.gateways.items():
            is_connected = gateway.is_connected
            db_status[name] = "connected" if is_connected else "disconnected"
            if not is_connected:
                all_healthy = False

        status = "healthy" if all_healthy and runtime.is_ready else "unhealthy"
        response = {
            "status": status,
            "ready": runtime.is_ready,
            "databases": db_status,
            "environment": env.app_env,
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
        if not runtime_of(app).is_ready:
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
        runtime = runtime_of(app)
        tables_info = {
            db_name: {"tables": schema.get_table_names(), "table_count": len(schema.tables)}
            for db_name, schema in runtime.schemas.items()
        }

        catalog_info: dict[str, Any] = {"available_catalogs": [], "catalog_count": 0}
        settings = runtime.settings
        if runtime.container is not None and settings is not None:
            try:
                names = runtime.container.repository.list_catalogs()
                catalog_info = {
                    "available_catalogs": names,
                    "catalog_count": len(names),
                    "storage_path": settings.settings.catalog.storage_path,
                }
            except Exception:
                pass

        cfg = settings.settings if settings else None
        return {
            "name": "Warp Engine",
            "version": __version__,
            "environment": env.app_env,
            "databases": tables_info,
            "catalog": catalog_info,
            "settings": {
                "api_prefix": cfg.api_prefix if cfg else "/api/v1",
                "pagination": {
                    "default_limit": cfg.pagination.default_limit if cfg else 50,
                    "max_limit": cfg.pagination.max_limit if cfg else 1000,
                },
                "raw_query_enabled": cfg.enable_raw_query if cfg else False,
            },
            "capabilities": capabilities_of(cfg, arrow_available()),
        }

    return app
