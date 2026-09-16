"""Dynamic router factory for generating CRUD endpoints for database tables."""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from warp.adapters.inbound.http.auth import AuthManager, Permission
from warp.adapters.outbound.db.base import DatabaseAdapter
from warp.application.services.crud import CRUDOperations
from warp.application.services.schema_discovery import SchemaAnalyzer
from warp.domain.errors import ValidationError
from warp.domain.filtering import parse_filters_from_request
from warp.domain.pagination import PaginatedResponse, PaginationParams
from warp.domain.schema import TableSchema
from warp.domain.sorting import parse_sort_from_request
from warp.domain.sql_types import type_kind

logger = logging.getLogger(__name__)


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
    except ValueError:
        expected = "an integer" if kind == "int" else "a number"
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
        db: DatabaseAdapter,
        schema_analyzer: SchemaAnalyzer,
        default_limit: int = 50,
        max_limit: int = 1000,
        db_name: str | None = None,
        auth_manager: AuthManager | None = None,
        readonly_columns: list[str] | None = None,
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
        """
        self.db = db
        self.analyzer = schema_analyzer
        self.default_limit = default_limit
        self.max_limit = max_limit
        self.db_name = db_name
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
        pk_column = table_schema.pk_column or "id"
        column_names = table_schema.get_column_names()

        # Column kinds drive id parsing and typed filter coercion.
        pk_col_schema = table_schema.get_column(pk_column)
        pk_kind = (
            type_kind(pk_col_schema.type, pk_col_schema.udt_name, pk_col_schema.full_type)
            if pk_col_schema
            else "int"
        )
        column_kinds = {
            c.name: type_kind(c.type, c.udt_name, c.full_type) for c in table_schema.columns
        }

        # Generate models if not provided
        if models is None:
            models = self.analyzer.generate_crud_models(table_schema)

        # Get or create CRUD instance
        crud = self._get_crud(table_schema)

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
            query_params = dict(request.query_params)
            try:
                filters = parse_filters_from_request(query_params, column_names, column_kinds)
                sort_fields = parse_sort_from_request(sort, column_names)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e)) from e

            columns = parse_fields(fields)

            pagination = PaginationParams(limit=limit, offset=offset)

            return await crud.get_all(
                columns=columns, filters=filters, pagination=pagination, sort=sort_fields
            )

        # GET BY ID endpoint
        @router.get(
            "/{id}",
            response_model=ResponseModel,
            summary=f"Get {table_name} by ID",
            description=f"Retrieve a single {table_name} record by its {pk_column}.",
            dependencies=get_auth_deps(Permission.READ),
        )
        async def get_record(
            id: str,
            fields: str | None = Query(
                default=None, description="Comma-separated list of fields to return"
            ),
        ) -> dict[str, Any]:
            typed_id = parse_id(id, pk_kind, pk_column)
            columns = parse_fields(fields)

            record = await crud.get_by_id(typed_id, columns=columns)

            if record is None:
                raise HTTPException(
                    status_code=404, detail=f"{table_name} with {pk_column}={id} not found"
                )

            return record

        # CREATE endpoint
        @router.post(
            "",
            response_model=ResponseModel,
            status_code=201,
            summary=f"Create {table_name}",
            description=f"Create a new {table_name} record.",
            dependencies=get_auth_deps(Permission.CREATE),
        )
        async def create_record(data: CreateModel) -> dict[str, Any]:
            try:
                record = await crud.create(data.model_dump(exclude_unset=True))
                return record
            except ValidationError as e:
                # Safe, intentional message (e.g. read-only column rejected).
                raise HTTPException(status_code=400, detail=e.message) from e
            except Exception as e:
                logger.exception(f"Failed to create {table_name} record: {e}")
                raise HTTPException(status_code=400, detail="Failed to create record") from e

        # UPDATE endpoint
        @router.put(
            "/{id}",
            response_model=ResponseModel,
            summary=f"Update {table_name}",
            description=f"Update an existing {table_name} record.",
            dependencies=get_auth_deps(Permission.UPDATE),
        )
        async def update_record(id: str, data: UpdateModel) -> dict[str, Any] | None:
            typed_id = parse_id(id, pk_kind, pk_column)

            # Check if exists
            if not await crud.exists(typed_id):
                raise HTTPException(
                    status_code=404, detail=f"{table_name} with {pk_column}={id} not found"
                )

            try:
                record = await crud.update(typed_id, data.model_dump(exclude_unset=True))
                return record
            except ValidationError as e:
                # Safe, intentional message (e.g. read-only column rejected).
                raise HTTPException(status_code=400, detail=e.message) from e
            except Exception as e:
                logger.exception(f"Failed to update {table_name} record: {e}")
                raise HTTPException(status_code=400, detail="Failed to update record") from e

        # PATCH endpoint (partial update)
        @router.patch(
            "/{id}",
            response_model=ResponseModel,
            summary=f"Partial update {table_name}",
            description=f"Partially update an existing {table_name} record.",
            dependencies=get_auth_deps(Permission.UPDATE),
        )
        async def patch_record(id: str, data: UpdateModel) -> dict[str, Any] | None:
            typed_id = parse_id(id, pk_kind, pk_column)

            # Check if exists
            if not await crud.exists(typed_id):
                raise HTTPException(
                    status_code=404, detail=f"{table_name} with {pk_column}={id} not found"
                )

            try:
                record = await crud.update(typed_id, data.model_dump(exclude_unset=True))
                return record
            except ValidationError as e:
                # Safe, intentional message (e.g. read-only column rejected).
                raise HTTPException(status_code=400, detail=e.message) from e
            except Exception as e:
                logger.exception(f"Failed to update {table_name} record: {e}")
                raise HTTPException(status_code=400, detail="Failed to update record") from e

        # DELETE endpoint
        @router.delete(
            "/{id}",
            status_code=204,
            summary=f"Delete {table_name}",
            description=f"Delete a {table_name} record by its {pk_column}.",
            dependencies=get_auth_deps(Permission.DELETE),
        )
        async def delete_record(id: str) -> None:
            typed_id = parse_id(id, pk_kind, pk_column)

            deleted = await crud.delete(typed_id)

            if not deleted:
                raise HTTPException(
                    status_code=404, detail=f"{table_name} with {pk_column}={id} not found"
                )

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

    def _get_crud(self, table_schema: TableSchema) -> CRUDOperations:
        """Get or create CRUD operations instance for a table."""
        table_name = table_schema.table_name

        if table_name not in self._crud_instances:
            self._crud_instances[table_name] = CRUDOperations(
                db=self.db, table_schema=table_schema, readonly_columns=self.readonly_columns
            )

        return self._crud_instances[table_name]
