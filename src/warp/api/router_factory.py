"""
Dynamic router factory for generating CRUD endpoints for database tables.
"""
from typing import Any, Dict, List, Optional, Type

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from ..database.base import DatabaseAdapter
from ..schema.analyzer import SchemaAnalyzer
from ..schema.models import TableSchema
from ..utils.filtering import parse_filters_from_request
from ..utils.pagination import PaginationParams, PaginatedResponse
from ..utils.sorting import parse_sort_from_request
from .auth import AuthManager, Permission
from .crud import CRUDOperations


def convert_id_type(value: str, column_type: str) -> Any:
    """
    Convert string id to the appropriate type based on column type.
    
    Args:
        value: The string value to convert.
        column_type: The database column type.
    
    Returns:
        The converted value.
    """
    column_type_lower = column_type.lower()
    
    # Integer types
    if any(t in column_type_lower for t in ['int', 'serial', 'bigint', 'smallint']):
        return int(value)
    
    # Float types
    if any(t in column_type_lower for t in ['float', 'double', 'decimal', 'numeric', 'real']):
        return float(value)
    
    # UUID type
    if 'uuid' in column_type_lower:
        return str(value)  # Keep as string for UUID
    
    # Default to string
    return value


class RouterFactory:
    """
    Factory for creating CRUD routers for database tables.

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
        db_name: Optional[str] = None,
        auth_manager: Optional[AuthManager] = None
    ):
        """
        Initialize the router factory.

        Args:
            db: Database adapter instance.
            schema_analyzer: Schema analyzer instance.
            default_limit: Default pagination limit.
            max_limit: Maximum allowed pagination limit.
            db_name: Optional database name for tag prefixing.
            auth_manager: Optional auth manager for permission control.
        """
        self.db = db
        self.analyzer = schema_analyzer
        self.default_limit = default_limit
        self.max_limit = max_limit
        self.db_name = db_name
        self.auth_manager = auth_manager
        self._crud_instances: Dict[str, CRUDOperations] = {}

    def create_router(
        self,
        table_schema: TableSchema,
        models: Optional[Dict[str, Type[BaseModel]]] = None
    ) -> APIRouter:
        """
        Create a CRUD router for a single table.

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
        
        # Get PK column type for proper type conversion
        pk_col_schema = table_schema.get_column(pk_column)
        pk_type = pk_col_schema.type if pk_col_schema else "integer"

        # Generate models if not provided
        if models is None:
            models = self.analyzer.generate_crud_models(table_schema)

        # Get or create CRUD instance
        crud = self._get_crud(table_schema)

        # Create router with optional db_name prefix in tags
        tag_name = table_name.replace("_", " ").title()
        if self.db_name:
            tag_name = f"{self.db_name} - {tag_name}"
        
        router = APIRouter(
            prefix=f"/{table_name}",
            tags=[tag_name]
        )

        # Response model
        ResponseModel = models.get("response", models.get("base"))
        CreateModel = models.get("create")
        UpdateModel = models.get("update")

        # Auth dependencies
        def get_auth_deps(permission: Permission) -> List:
            if self.auth_manager and self.auth_manager.enabled:
                return [Depends(self.auth_manager.require(permission))]
            return []

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
            """
        )
        async def list_records(
            request: Request,
            limit: int = Query(
                default=self.default_limit,
                ge=1,
                le=self.max_limit,
                description="Number of records to return"
            ),
            offset: int = Query(
                default=0,
                ge=0,
                description="Number of records to skip"
            ),
            sort: Optional[str] = Query(
                default=None,
                description="Sort order (e.g., 'name:asc,created_at:desc')"
            ),
            fields: Optional[str] = Query(
                default=None,
                description="Comma-separated list of fields to return"
            )
        ):
            # Parse query params for filters
            query_params = dict(request.query_params)
            filters = parse_filters_from_request(query_params, column_names)

            # Parse sort
            sort_fields = parse_sort_from_request(sort, column_names)

            # Parse fields
            columns = None
            if fields:
                columns = [f.strip() for f in fields.split(",")]
                # Validate columns
                invalid = set(columns) - set(column_names)
                if invalid:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Invalid fields: {', '.join(invalid)}"
                    )

            pagination = PaginationParams(limit=limit, offset=offset)

            return await crud.get_all(
                columns=columns,
                filters=filters,
                pagination=pagination,
                sort=sort_fields
            )

        # GET BY ID endpoint
        @router.get(
            "/{id}",
            response_model=ResponseModel,
            summary=f"Get {table_name} by ID",
            description=f"Retrieve a single {table_name} record by its {pk_column}.",
            dependencies=get_auth_deps(Permission.READ)
        )
        async def get_record(
            id: str,
            fields: Optional[str] = Query(
                default=None,
                description="Comma-separated list of fields to return"
            )
        ):
            # Convert id to proper type
            typed_id = convert_id_type(id, pk_type)
            
            # Parse fields
            columns = None
            if fields:
                columns = [f.strip() for f in fields.split(",")]

            record = await crud.get_by_id(typed_id, columns=columns)

            if record is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"{table_name} with {pk_column}={id} not found"
                )

            return record

        # CREATE endpoint
        @router.post(
            "",
            response_model=ResponseModel,
            status_code=201,
            summary=f"Create {table_name}",
            description=f"Create a new {table_name} record.",
            dependencies=get_auth_deps(Permission.CREATE)
        )
        async def create_record(data: CreateModel):
            try:
                record = await crud.create(data.model_dump(exclude_unset=True))
                return record
            except Exception as e:
                raise HTTPException(
                    status_code=400,
                    detail=f"Failed to create record: {str(e)}"
                )

        # UPDATE endpoint
        @router.put(
            "/{id}",
            response_model=ResponseModel,
            summary=f"Update {table_name}",
            description=f"Update an existing {table_name} record.",
            dependencies=get_auth_deps(Permission.UPDATE)
        )
        async def update_record(id: str, data: UpdateModel):
            # Convert id to proper type
            typed_id = convert_id_type(id, pk_type)
            
            # Check if exists
            if not await crud.exists(typed_id):
                raise HTTPException(
                    status_code=404,
                    detail=f"{table_name} with {pk_column}={id} not found"
                )

            try:
                record = await crud.update(typed_id, data.model_dump(exclude_unset=True))
                return record
            except Exception as e:
                raise HTTPException(
                    status_code=400,
                    detail=f"Failed to update record: {str(e)}"
                )

        # PATCH endpoint (partial update)
        @router.patch(
            "/{id}",
            response_model=ResponseModel,
            summary=f"Partial update {table_name}",
            description=f"Partially update an existing {table_name} record.",
            dependencies=get_auth_deps(Permission.UPDATE)
        )
        async def patch_record(id: str, data: UpdateModel):
            # Convert id to proper type
            typed_id = convert_id_type(id, pk_type)
            
            # Check if exists
            if not await crud.exists(typed_id):
                raise HTTPException(
                    status_code=404,
                    detail=f"{table_name} with {pk_column}={id} not found"
                )

            try:
                record = await crud.update(typed_id, data.model_dump(exclude_unset=True))
                return record
            except Exception as e:
                raise HTTPException(
                    status_code=400,
                    detail=f"Failed to update record: {str(e)}"
                )

        # DELETE endpoint
        @router.delete(
            "/{id}",
            status_code=204,
            summary=f"Delete {table_name}",
            description=f"Delete a {table_name} record by its {pk_column}.",
            dependencies=get_auth_deps(Permission.DELETE)
        )
        async def delete_record(id: str):
            # Convert id to proper type
            typed_id = convert_id_type(id, pk_type)
            
            deleted = await crud.delete(typed_id)

            if not deleted:
                raise HTTPException(
                    status_code=404,
                    detail=f"{table_name} with {pk_column}={id} not found"
                )

            return None

        return router

    def create_routers_for_all_tables(
        self,
        table_schemas: Dict[str, TableSchema]
    ) -> List[APIRouter]:
        """
        Create routers for all tables.

        Args:
            table_schemas: Dictionary of table name to TableSchema.

        Returns:
            List of FastAPI routers.
        """
        routers = []

        for table_name, schema in table_schemas.items():
            router = self.create_router(schema)
            routers.append(router)

        return routers

    def _get_crud(self, table_schema: TableSchema) -> CRUDOperations:
        """Get or create CRUD operations instance for a table."""
        table_name = table_schema.table_name

        if table_name not in self._crud_instances:
            self._crud_instances[table_name] = CRUDOperations(
                db=self.db,
                table_schema=table_schema
            )

        return self._crud_instances[table_name]
