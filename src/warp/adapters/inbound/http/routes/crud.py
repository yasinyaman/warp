"""Dynamic router factory for generating CRUD endpoints for database tables."""

import inspect
import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any, TypeVar
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from warp.adapters.inbound.http.auth import (
    AuthManager,
    Permission,
    caller_of,
    policy_of,
    roles_of,
)
from warp.adapters.inbound.http.governance import NO_GOVERNANCE, Governance
from warp.adapters.inbound.http.request_context import request_id_of
from warp.application.ports.database import DatabaseGateway
from warp.application.services.crud import CRUDOperations
from warp.application.services.schema_discovery import SchemaAnalyzer
from warp.domain.audit import AuditEvent
from warp.domain.errors import ValidationError
from warp.domain.filtering import parse_filters_from_request
from warp.domain.masking import mask_row, mask_rows
from warp.domain.pagination import PaginatedResponse, PaginationParams
from warp.domain.schema import TableSchema
from warp.domain.sorting import parse_sort_from_request
from warp.domain.sql_types import type_kind

logger = logging.getLogger(__name__)


#: A route handler. Named so the key-route decorator can promise it returns
#: the same function it was given, which keeps the handlers typed.
RouteFn = TypeVar("RouteFn", bound=Callable[..., Any])


def parse_id(value: str, kind: str, column: str = "id") -> Any:
    """Convert a path id to the primary key's kind, or fail with 422.

    Args:
        value: Raw path segment.
        kind: Column kind from ``type_kind`` (``int``, ``float``, ``str``...).
        column: Primary-key column name (for the error message).

    Returns:
        The converted id (strings, including UUIDs, pass through).

    Raises:
        HTTPException: 422 when the text cannot be converted to the kind.
    """
    try:
        if kind == "int":
            return int(value)
        if kind == "float":
            return float(value)
        if kind == "uuid":
            return UUID(value)
    except ValueError:
        expected = {"int": "an integer", "float": "a number", "uuid": "a UUID"}[kind]
        raise HTTPException(
            status_code=422, detail=f"Invalid {column}: expected {expected}, got {value!r}"
        ) from None
    return value


class RouterFactory:
    """Factory for creating CRUD routers for database tables.

    Automatically generates REST endpoints for each table:
    - GET /{table} - List with pagination, filtering, sorting
    - GET /{table}/{id} - Get single record
    - POST /{table} - Create new record
    - PUT /{table}/{id} - Update record
    - DELETE /{table}/{id} - Delete record
    """

    def __init__(
        self,
        db: DatabaseGateway,
        schema_analyzer: SchemaAnalyzer,
        default_limit: int = 50,
        max_limit: int = 1000,
        db_name: str | None = None,
        auth_manager: AuthManager | None = None,
        readonly_columns: list[str] | None = None,
        governance: Governance | None = None,
    ):
        """Initialize the router factory.

        Args:
            db: Database adapter instance.
            schema_analyzer: Schema analyzer instance.
            default_limit: Default pagination limit.
            max_limit: Maximum allowed pagination limit.
            db_name: Optional database name for tag prefixing.
            auth_manager: Optional auth manager for permission control.
            readonly_columns: Columns clients may never write (mass-assignment).
            governance: Column masking and the audit sink.
        """
        self.db = db
        self.analyzer = schema_analyzer
        self.default_limit = default_limit
        self.max_limit = max_limit
        self.db_name = db_name
        self.governance = governance or NO_GOVERNANCE
        self.masking = self.governance.masking
        self.audit = self.governance.audit
        self.auth_manager = auth_manager
        self.readonly_columns = readonly_columns or []
        self._crud_instances: dict[str, CRUDOperations] = {}

    def create_router(  # noqa: C901, PLR0915
        self, table_schema: TableSchema, models: dict[str, type[BaseModel]] | None = None
    ) -> APIRouter:
        """Create a CRUD router for a single table.

        Args:
            table_schema: Schema of the table.
            models: Optional dictionary of Pydantic models
                   (keys: 'create', 'update', 'response').

        Returns:
            FastAPI router with CRUD endpoints.
        """
        table_name = table_schema.table_name
        key_columns = table_schema.key_columns
        column_names = table_schema.get_column_names()

        # One path segment per key column, so a composite key cannot be
        # collapsed to its first: `/{siparis_id}/{satir_no}`. A single-key
        # table is unchanged on the wire.
        key_path = "".join(f"/{{{column}}}" for column in key_columns) or "/{id}"

        # Column kinds drive key parsing and typed filter coercion.
        def kind_of(column: str) -> str:
            schema = table_schema.get_column(column)
            return type_kind(schema.type, schema.udt_name, schema.full_type) if schema else "int"

        key_kinds = {column: kind_of(column) for column in key_columns}

        def parse_key(raw: Mapping[str, str]) -> dict[str, Any]:
            """Coerce the path segments to their columns' types, in key order."""
            return {
                column: parse_id(raw[column], key_kinds[column], column) for column in key_columns
            }

        def key_text(key: Mapping[str, Any]) -> str:
            return ", ".join(f"{column}={key[column]}" for column in key_columns)

        def key_route(method: Callable[..., Any], **options: Any) -> Callable[[RouteFn], RouteFn]:
            """Register a route addressed by the whole primary key.

            A table without one registers nothing: there is no way to address
            a single row, and the fabricated ``id`` column this used to fall
            back on was a guess that reached the database.
            """
            if not key_columns:
                return lambda fn: fn
            register = method(key_path, **options)

            def decorate(fn: RouteFn) -> RouteFn:
                # FastAPI reads path parameters off the signature, so the key
                # columns are declared there rather than caught as **kwargs.
                declared = [
                    parameter
                    for parameter in inspect.signature(fn).parameters.values()
                    if parameter.kind is not inspect.Parameter.VAR_KEYWORD
                ]
                # Set rather than declared: `__signature__` is how FastAPI is
                # told what the path parameters are, and it is not part of the
                # Callable type.
                fn.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
                    [
                        *declared,
                        *(
                            inspect.Parameter(
                                column, inspect.Parameter.KEYWORD_ONLY, annotation=str
                            )
                            for column in key_columns
                        ),
                    ]
                )
                register(fn)
                return fn

            return decorate

        column_kinds = {
            c.name: type_kind(c.type, c.udt_name, c.full_type) for c in table_schema.columns
        }

        # Generate models if not provided
        if models is None:
            models = self.analyzer.generate_crud_models(table_schema)

        # Get or create CRUD instance
        base_crud = self._get_crud(table_schema)

        # Create router with optional db_name prefix in tags
        tag_name = table_name.replace("_", " ").title()
        if self.db_name:
            tag_name = f"{self.db_name} - {tag_name}"

        router = APIRouter(prefix=f"/{table_name}", tags=[tag_name])

        # Response model
        ResponseModel = models.get("response", models.get("base"))
        # CreateModel / UpdateModel are dynamically generated Pydantic models.
        # They are used as runtime annotations on the route handlers below, so
        # FastAPI can parse request bodies; mypy cannot treat a runtime variable
        # as a static type, hence the Any annotation.
        CreateModel: Any = models.get("create")
        UpdateModel: Any = models.get("update")

        # Auth dependencies
        def get_auth_deps(permission: Permission) -> list[Any]:
            if self.auth_manager and self.auth_manager.enabled:
                return [Depends(self.auth_manager.require(permission))]
            return []

        def parse_fields(fields: str | None) -> list[str] | None:
            """Validate a comma-separated `fields` selection against the schema."""
            if not fields:
                return None
            columns = [f.strip() for f in fields.split(",") if f.strip()]
            invalid = set(columns) - set(column_names)
            if invalid:
                raise HTTPException(
                    status_code=400, detail=f"Invalid fields: {', '.join(sorted(invalid))}"
                )
            return columns or None

        # LIST endpoint
        @router.get(
            "",
            response_model=PaginatedResponse,
            summary=f"List {table_name}",
            dependencies=get_auth_deps(Permission.READ),
            description=f"""
Retrieve a paginated list of {table_name} records.

**Filtering:**
- `filter[column]=value` - Exact match
- `filter[column][gt]=value` - Greater than
- `filter[column][gte]=value` - Greater than or equal
- `filter[column][lt]=value` - Less than
- `filter[column][lte]=value` - Less than or equal
- `filter[column][like]=%value%` - Pattern match
- `filter[column][in]=val1,val2` - In list
- `filter[column][is_null]=true` - Is null check

**Sorting:**
- `sort=column:asc` - Ascending
- `sort=column:desc` - Descending
- `sort=-column` - Descending (prefix notation)
- `sort=col1:asc,col2:desc` - Multiple fields

**Pagination:**
- `limit=50` - Records per page (default: {self.default_limit}, max: {self.max_limit})
- `offset=0` - Number of records to skip
            """,
        )
        async def list_records(
            request: Request,
            limit: int = Query(
                default=self.default_limit,
                ge=1,
                le=self.max_limit,
                description="Number of records to return",
            ),
            offset: int = Query(default=0, ge=0, description="Number of records to skip"),
            sort: str | None = Query(
                default=None, description="Sort order (e.g., 'name:asc,created_at:desc')"
            ),
            fields: str | None = Query(
                default=None, description="Comma-separated list of fields to return"
            ),
        ) -> PaginatedResponse[Any]:
            # Filters (typed by column kind) and sort; both validate against the schema.
            crud = base_crud.with_policy(policy_of(request))
            query_params = dict(request.query_params)
            try:
                filters = parse_filters_from_request(query_params, column_names, column_kinds)
                sort_fields = parse_sort_from_request(sort, column_names)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e)) from e

            columns = parse_fields(fields)

            pagination = PaginationParams(limit=limit, offset=offset)

            page = await crud.get_all(
                columns=columns, filters=filters, pagination=pagination, sort=sort_fields
            )
            masks = self.masking.masks_for(table_name, roles_of(request))
            if masks:
                page.items = mask_rows(page.items, masks, self.masking.hash_key)
            self._record(
                request,
                "read",
                table_name,
                row_count=len(page.items),
                masks=masks,
                filtered_columns=[f[0] for f in filters],
            )
            return page

        # GET BY ID endpoint
        @key_route(
            router.get,
            response_model=ResponseModel,
            summary=f"Get {table_name} by ID",
            description=f"Retrieve a single {table_name} record by its primary key.",
            dependencies=get_auth_deps(Permission.READ),
        )
        async def get_record(
            request: Request,
            fields: str | None = Query(
                default=None, description="Comma-separated list of fields to return"
            ),
            **raw: str,
        ) -> dict[str, Any]:
            crud = base_crud.with_policy(policy_of(request))
            key = parse_key(raw)
            columns = parse_fields(fields)

            record = await crud.get_by_id(key, columns=columns)

            if record is None:
                # Recorded too: a caller probing for rows they cannot see is
                # exactly what an audit trail is for.
                self._record(request, "read", table_name, status=404, row_count=0)
                raise HTTPException(
                    status_code=404, detail=f"{table_name} with {key_text(key)} not found"
                )

            masks = self.masking.masks_for(table_name, roles_of(request))
            self._record(request, "read", table_name, row_count=1, masks=masks)
            return mask_row(record, masks, self.masking.hash_key)

        # CREATE endpoint
        @router.post(
            "",
            response_model=ResponseModel,
            status_code=201,
            summary=f"Create {table_name}",
            description=f"Create a new {table_name} record.",
            dependencies=get_auth_deps(Permission.CREATE),
        )
        async def create_record(request: Request, data: CreateModel) -> dict[str, Any]:
            crud = base_crud.with_policy(policy_of(request))
            try:
                record = await crud.create(data.model_dump(exclude_unset=True))
            except ValidationError as e:
                # Safe, intentional message (e.g. read-only column rejected).
                self._record(request, "create", table_name, status=400)
                raise HTTPException(status_code=400, detail=e.message) from e
            except Exception as e:
                logger.exception(f"Failed to create {table_name} record: {e}")
                self._record(request, "create", table_name, status=400)
                raise HTTPException(status_code=400, detail="Failed to create record") from e
            self._record(request, "create", table_name, status=201, row_count=1)
            return record

        # UPDATE endpoint
        @key_route(
            router.put,
            response_model=ResponseModel,
            summary=f"Update {table_name}",
            description=f"Update an existing {table_name} record.",
            dependencies=get_auth_deps(Permission.UPDATE),
        )
        async def update_record(
            request: Request, data: UpdateModel, **raw: str
        ) -> dict[str, Any] | None:
            crud = base_crud.with_policy(policy_of(request))
            key = parse_key(raw)

            # Check if exists
            if not await crud.exists(key):
                self._record(request, "update", table_name, status=404, row_count=0)
                raise HTTPException(
                    status_code=404, detail=f"{table_name} with {key_text(key)} not found"
                )

            try:
                record = await crud.update(key, data.model_dump(exclude_unset=True))
            except ValidationError as e:
                # Safe, intentional message (e.g. read-only column rejected).
                self._record(request, "update", table_name, status=400)
                raise HTTPException(status_code=400, detail=e.message) from e
            except Exception as e:
                logger.exception(f"Failed to update {table_name} record: {e}")
                self._record(request, "update", table_name, status=400)
                raise HTTPException(status_code=400, detail="Failed to update record") from e
            self._record(request, "update", table_name, row_count=1 if record else 0)
            return record

        # PATCH endpoint (partial update)
        @key_route(
            router.patch,
            response_model=ResponseModel,
            summary=f"Partial update {table_name}",
            description=f"Partially update an existing {table_name} record.",
            dependencies=get_auth_deps(Permission.UPDATE),
        )
        async def patch_record(
            request: Request, data: UpdateModel, **raw: str
        ) -> dict[str, Any] | None:
            crud = base_crud.with_policy(policy_of(request))
            key = parse_key(raw)

            # Check if exists
            if not await crud.exists(key):
                self._record(request, "update", table_name, status=404, row_count=0)
                raise HTTPException(
                    status_code=404, detail=f"{table_name} with {key_text(key)} not found"
                )

            try:
                record = await crud.update(key, data.model_dump(exclude_unset=True))
            except ValidationError as e:
                # Safe, intentional message (e.g. read-only column rejected).
                self._record(request, "update", table_name, status=400)
                raise HTTPException(status_code=400, detail=e.message) from e
            except Exception as e:
                logger.exception(f"Failed to update {table_name} record: {e}")
                self._record(request, "update", table_name, status=400)
                raise HTTPException(status_code=400, detail="Failed to update record") from e
            self._record(request, "update", table_name, row_count=1 if record else 0)
            return record

        # DELETE endpoint
        @key_route(
            router.delete,
            status_code=204,
            summary=f"Delete {table_name}",
            description=f"Delete a {table_name} record by its primary key.",
            dependencies=get_auth_deps(Permission.DELETE),
        )
        async def delete_record(request: Request, **raw: str) -> None:
            crud = base_crud.with_policy(policy_of(request))
            key = parse_key(raw)

            deleted = await crud.delete(key)

            if not deleted:
                self._record(request, "delete", table_name, status=404, row_count=0)
                raise HTTPException(
                    status_code=404, detail=f"{table_name} with {key_text(key)} not found"
                )
            self._record(request, "delete", table_name, status=204, row_count=1)

        return router

    def create_routers_for_all_tables(
        self, table_schemas: dict[str, TableSchema]
    ) -> list[APIRouter]:
        """Create routers for all tables.

        Args:
            table_schemas: Dictionary of table name to TableSchema.

        Returns:
            List of FastAPI routers.
        """
        routers = []

        for _table_name, schema in table_schemas.items():
            router = self.create_router(schema)
            routers.append(router)

        return routers

    def _record(
        self,
        request: Request,
        action: str,
        table_name: str,
        *,
        status: int = 200,
        row_count: int | None = None,
        masks: Mapping[str, str] | None = None,
        filtered_columns: Sequence[str] = (),
    ) -> None:
        """Record one data access. Never raises; auditing must not fail a request."""
        caller = caller_of(request)
        self.audit.record(
            AuditEvent(
                action=action,  # type: ignore[arg-type]
                database=self.db_name or "default",
                table=table_name,
                actor=caller.name if caller else None,
                tenant=caller.tenant if caller else None,
                roles=tuple(caller.roles) if caller else (),
                request_id=request_id_of(request),
                status=status,
                row_count=row_count,
                row_filtered=policy_of(request).covers(table_name),
                masked_columns=tuple(sorted(masks or {})),
                filtered_columns=tuple(filtered_columns),
            )
        )

    def _get_crud(self, table_schema: TableSchema) -> CRUDOperations:
        """Get or create CRUD operations instance for a table."""
        table_name = table_schema.table_name

        if table_name not in self._crud_instances:
            self._crud_instances[table_name] = CRUDOperations(
                db=self.db, table_schema=table_schema, readonly_columns=self.readonly_columns
            )

        return self._crud_instances[table_name]
