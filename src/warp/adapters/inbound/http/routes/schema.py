"""Typed schema endpoints: ``GET /schema`` and ``GET /{table}/schema``.

Clients that load data elsewhere (an OLAP engine, an ETL job) need column
types, nullability, the primary key and a cheap row estimate before they
touch any rows. The information comes from the schema discovered at startup
plus planner statistics; nothing here scans a table.
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from warp.adapters.inbound.http.auth import AuthManager, Permission
from warp.application.ports.database import DatabaseGateway
from warp.domain.schema import ColumnSchema, DatabaseSchema, TableSchema
from warp.domain.sql_types import type_kind


class ColumnOut(BaseModel):
    """A column as reported by the database, plus its coarse ``kind``."""

    name: str
    type: str
    full_type: str | None = None
    udt_name: str | None = None
    kind: str = Field(description="int/float/bool/str/json/bytes/list/datetime/date/time/uuid")
    nullable: bool = True
    precision: int | None = None
    scale: int | None = None
    max_length: int | None = None
    default: str | None = None


class TableSchemaOut(BaseModel):
    """Columns, primary key and planner row estimate of one table."""

    table: str
    columns: list[ColumnOut]
    primary_key: list[str] = Field(default_factory=list)
    row_estimate: int | None = Field(
        default=None, description="Planner statistics, not a COUNT(*); null when unknown"
    )


class DatabaseSchemaOut(BaseModel):
    """Every discovered table of a database."""

    database: str
    tables: dict[str, TableSchemaOut]


def column_out(column: ColumnSchema) -> ColumnOut:
    """Convert a discovered column to its wire representation."""
    return ColumnOut(
        name=column.name,
        type=column.type,
        full_type=column.full_type,
        udt_name=column.udt_name,
        kind=type_kind(column.type, column.udt_name, column.full_type),
        nullable=column.nullable,
        precision=column.precision,
        scale=column.scale,
        max_length=column.max_length,
        default=None if column.default is None else str(column.default),
    )


def primary_key_columns(schema: TableSchema) -> list[str]:
    """The primary key as a list (empty when the table has none)."""
    if isinstance(schema.primary_key, str):
        return [schema.primary_key]
    return list(schema.primary_key or [])


def table_schema_out(schema: TableSchema, row_estimate: int | None) -> TableSchemaOut:
    """Convert a discovered table schema to its wire representation."""
    return TableSchemaOut(
        table=schema.table_name,
        columns=[column_out(c) for c in schema.columns],
        primary_key=primary_key_columns(schema),
        row_estimate=row_estimate,
    )


def create_schema_router(
    db_name: str,
    schema: DatabaseSchema,
    gateway: DatabaseGateway,
    auth_manager: AuthManager | None = None,
) -> APIRouter:
    """Build the schema router for one database.

    Mount it *before* the per-table CRUD routers: ``GET /{table}/schema`` must
    win over the CRUD ``GET /{table}/{id}`` route (a table literally named
    ``schema`` is shadowed by ``GET /schema``).

    Args:
        db_name: Database name (reported in the payload).
        schema: The schema discovered at startup.
        gateway: Used only for planner row estimates.
        auth_manager: When enabled, both routes need the ``read`` permission.

    Returns:
        The router.
    """
    router = APIRouter(tags=[f"{db_name} - Schema"])

    deps: list[Any] = []
    if auth_manager and auth_manager.enabled:
        deps = [Depends(auth_manager.require(Permission.READ))]

    async def estimates_for(tables: list[str], wanted: bool) -> dict[str, int | None]:
        if not wanted or not tables:
            return {}
        return await gateway.row_estimates(tables)

    @router.get(
        "/schema",
        response_model=DatabaseSchemaOut,
        summary="Database schema",
        description="Typed columns, primary keys and planner row estimates of every table.",
        dependencies=deps,
    )
    async def database_schema(
        estimates: bool = Query(default=True, description="Include planner row estimates"),
    ) -> DatabaseSchemaOut:
        found = await estimates_for(list(schema.tables), estimates)
        return DatabaseSchemaOut(
            database=db_name,
            tables={
                name: table_schema_out(table, found.get(name))
                for name, table in schema.tables.items()
            },
        )

    @router.get(
        "/{table}/schema",
        response_model=TableSchemaOut,
        summary="Table schema",
        description="Typed columns, primary key and planner row estimate of one table.",
        dependencies=deps,
    )
    async def table_schema(
        table: str,
        estimates: bool = Query(default=True, description="Include the planner row estimate"),
    ) -> TableSchemaOut:
        found = schema.get_table(table)
        if found is None:
            raise HTTPException(status_code=404, detail=f"Table '{table}' not found")
        estimate = (await estimates_for([table], estimates)).get(table)
        return table_schema_out(found, estimate)

    return router
