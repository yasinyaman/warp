"""Streaming export: ``GET|POST /{table}/export``.

Rows are pulled from a server-side cursor in batches and written to the
response as they arrive, so exporting a large table never buffers it in the
API process. Three formats: ``json`` (one ``{"items": [...], "row_count": N}``
document written incrementally), ``ndjson`` (one object per line) and
``arrow`` (Arrow IPC stream; needs ``warp-engine[arrow]``).

``GET`` takes the same ``fields`` / ``filter[col][op]`` / ``sort`` query
parameters as the list endpoint; ``POST`` takes a JSON body, which is the
only practical way to send long ``in`` lists.
"""

import base64
import json
import logging
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from warp.adapters.inbound.http.arrow_export import arrow_available, arrow_ipc_stream
from warp.adapters.inbound.http.auth import AuthManager, Permission
from warp.application.config import ExportConfig
from warp.application.ports.database import DatabaseGateway
from warp.domain.filtering import parse_filter_conditions, parse_filters_from_request
from warp.domain.schema import TableSchema
from warp.domain.sorting import parse_sort_from_request
from warp.domain.sql_types import type_kind

logger = logging.getLogger(__name__)

ExportFormat = Literal["json", "ndjson", "arrow"]
FilterOp = Literal["eq", "ne", "gt", "gte", "lt", "lte", "like", "in", "is_null"]

MEDIA_TYPES: dict[str, str] = {
    "json": "application/json",
    "ndjson": "application/x-ndjson",
    "arrow": "application/vnd.apache.arrow.stream",
}


class ExportFilter(BaseModel):
    """One filter condition of a ``POST`` export request."""

    column: str
    op: FilterOp = "eq"
    value: Any = None


class ExportSort(BaseModel):
    """One sort key of a ``POST`` export request."""

    column: str
    direction: Literal["asc", "desc"] = "asc"


class ExportRequest(BaseModel):
    """Body of ``POST /{table}/export``."""

    fields: list[str] | None = Field(default=None, description="Columns to export (all if omitted)")
    filters: list[ExportFilter] = Field(default_factory=list)
    sort: list[ExportSort] = Field(default_factory=list)
    limit: int | None = Field(default=None, ge=1, description="Maximum rows")
    format: ExportFormat = "json"


@dataclass(frozen=True)
class ExportPlan:
    """A validated export request, ready to be streamed."""

    columns: list[str] | None
    filters: list[tuple[str, str, Any]]
    sort: list[tuple[str, str]]
    limit: int | None
    capped: bool
    format: str


def _json_default(value: Any) -> Any:
    """Encode driver-native values that ``json`` does not know."""
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if isinstance(value, Decimal | UUID | timedelta):
        return str(value)
    if isinstance(value, bytes | bytearray | memoryview):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, set | frozenset):
        return sorted(value, key=str)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _dumps(row: dict[str, Any]) -> str:
    return json.dumps(row, default=_json_default, ensure_ascii=False, separators=(",", ":"))


async def json_document(batches: AsyncIterator[list[dict[str, Any]]]) -> AsyncIterator[bytes]:
    """Write ``{"items": [...], "row_count": N}`` incrementally."""
    yield b'{"items":['
    count = 0
    async for batch in batches:
        if not batch:
            continue
        chunk = ",".join(_dumps(row) for row in batch)
        yield (("," if count else "") + chunk).encode("utf-8")
        count += len(batch)
    yield f'],"row_count":{count}}}'.encode()


async def ndjson_lines(batches: AsyncIterator[list[dict[str, Any]]]) -> AsyncIterator[bytes]:
    """Write one JSON object per line."""
    async for batch in batches:
        if batch:
            yield ("\n".join(_dumps(row) for row in batch) + "\n").encode("utf-8")


def effective_limit(requested: int | None, max_rows: int) -> tuple[int | None, bool]:
    """Apply the configured cap; the flag says whether the cap changed the request."""
    if max_rows <= 0:
        return requested, False
    if requested is None or requested > max_rows:
        return max_rows, True
    return requested, False


def _split_fields(fields: str | None) -> list[str] | None:
    if not fields:
        return None
    return [f.strip() for f in fields.split(",") if f.strip()] or None


@dataclass
class TableExport:
    """Request validation and response streaming for one table's export routes."""

    table_schema: TableSchema
    gateway: DatabaseGateway
    export: ExportConfig

    def __post_init__(self) -> None:
        """Cache the column names/kinds used to validate every request."""
        self.table_name = self.table_schema.table_name
        self.column_names = self.table_schema.get_column_names()
        self.column_kinds = {
            c.name: type_kind(c.type, c.udt_name, c.full_type) for c in self.table_schema.columns
        }

    def plan(
        self,
        fields: list[str] | None,
        filters: list[tuple[str, str, Any]],
        sort: list[tuple[str, str]],
        limit: int | None,
        fmt: str,
    ) -> ExportPlan:
        """Validate the projection and apply the row cap."""
        invalid = sorted(set(fields or []) - set(self.column_names))
        if invalid:
            raise HTTPException(status_code=400, detail=f"Invalid fields: {', '.join(invalid)}")
        effective, capped = effective_limit(limit, self.export.max_rows)
        return ExportPlan(fields or None, filters, sort, effective, capped, fmt)

    def plan_from_query(
        self,
        query_params: Mapping[str, str],
        fields: str | None,
        sort: str | None,
        limit: int | None,
        fmt: str,
    ) -> ExportPlan:
        """Plan a ``GET`` export from list-style query parameters."""
        try:
            filters = parse_filters_from_request(
                dict(query_params), self.column_names, self.column_kinds
            )
            sort_fields = parse_sort_from_request(sort, self.column_names)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return self.plan(_split_fields(fields), filters, sort_fields, limit, fmt)

    def plan_from_body(self, body: ExportRequest) -> ExportPlan:
        """Plan a ``POST`` export from its JSON body."""
        try:
            filters = parse_filter_conditions(
                [(f.column, f.op, f.value) for f in body.filters],
                self.column_names,
                self.column_kinds,
            )
            sort_spec = ",".join(f"{s.column}:{s.direction}" for s in body.sort) or None
            sort_fields = parse_sort_from_request(sort_spec, self.column_names)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return self.plan(body.fields, filters, sort_fields, body.limit, body.format)

    def body_for(self, item: ExportPlan) -> AsyncIterator[bytes]:
        """The encoded byte stream for a plan (rows are not read until iterated)."""
        if item.format == "arrow" and not arrow_available():
            raise HTTPException(
                status_code=501,
                detail="Arrow export requires pyarrow: pip install 'warp-engine[arrow]'",
            )
        batches = self.gateway.stream_select(
            self.table_name,
            columns=item.columns,
            filters=item.filters,
            sort=item.sort,
            batch_size=self.export.batch_size,
            limit=item.limit,
            statement_timeout_ms=self.export.statement_timeout_ms,
        )
        if item.format == "arrow":
            return arrow_ipc_stream(batches, self.table_schema, item.columns)
        if item.format == "ndjson":
            return ndjson_lines(batches)
        return json_document(batches)

    def respond(self, item: ExportPlan) -> StreamingResponse:
        """Build the streaming response with the export headers."""
        headers = {"X-Export-Format": item.format, "Cache-Control": "no-store"}
        if item.capped and item.limit is not None:
            headers["X-Export-Max-Rows"] = str(item.limit)
        return StreamingResponse(
            self.body_for(item), media_type=MEDIA_TYPES[item.format], headers=headers
        )


def _read_dependencies(auth_manager: AuthManager | None) -> list[Any]:
    if auth_manager and auth_manager.enabled:
        return [Depends(auth_manager.require(Permission.READ))]
    return []


def _add_disabled_routes(router: APIRouter, table_name: str, deps: list[Any]) -> None:
    @router.get("/export", summary=f"Export {table_name} (disabled)", dependencies=deps)
    @router.post("/export", summary=f"Export {table_name} (disabled)", dependencies=deps)
    async def export_disabled() -> None:
        raise HTTPException(status_code=403, detail="Export is disabled")


def create_export_router(
    table_schema: TableSchema,
    gateway: DatabaseGateway,
    export: ExportConfig,
    auth_manager: AuthManager | None = None,
    db_name: str | None = None,
) -> APIRouter:
    """Build the export router for one table (prefix ``/{table}``).

    Mount it before the table's CRUD router so ``/{table}/export`` is not
    captured by ``GET /{table}/{id}``.

    Args:
        table_schema: The table's discovered schema (validates fields/filters).
        gateway: Streams the rows.
        export: Export settings (enabled, caps, batch size, timeout).
        auth_manager: When enabled, both routes need the ``read`` permission.
        db_name: Prefix for the OpenAPI tag when several databases are mounted.

    Returns:
        The router.
    """
    table_name = table_schema.table_name
    tag = table_name.replace("_", " ").title()
    if db_name:
        tag = f"{db_name} - {tag}"
    router = APIRouter(prefix=f"/{table_name}", tags=[tag])
    deps = _read_dependencies(auth_manager)

    if not export.enabled:
        _add_disabled_routes(router, table_name, deps)
        return router

    handler = TableExport(table_schema, gateway, export)

    @router.get(
        "/export",
        summary=f"Export {table_name}",
        description=(
            "Stream matching rows. Same `fields`, `filter[column][op]` and `sort` "
            "parameters as the list endpoint; `format` is `json` (default), `ndjson` "
            "or `arrow`. Use POST for long `in` lists."
        ),
        dependencies=deps,
        response_class=StreamingResponse,
    )
    async def export_get(
        request: Request,
        fields: str | None = Query(default=None, description="Comma-separated columns"),
        sort: str | None = Query(default=None, description="e.g. 'id:asc,name:desc'"),
        limit: int | None = Query(default=None, ge=1, description="Maximum rows"),
        format: ExportFormat = Query(default="json"),  # noqa: A002 - API parameter name
    ) -> StreamingResponse:
        return handler.respond(
            handler.plan_from_query(request.query_params, fields, sort, limit, format)
        )

    @router.post(
        "/export",
        summary=f"Export {table_name} (JSON body)",
        description="Stream matching rows; the body carries fields, filters, sort and limit.",
        dependencies=deps,
        response_class=StreamingResponse,
    )
    async def export_post(body: ExportRequest) -> StreamingResponse:
        return handler.respond(handler.plan_from_body(body))

    return router
