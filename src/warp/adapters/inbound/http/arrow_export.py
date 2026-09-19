"""Arrow IPC support for the streaming export endpoint.

``pyarrow`` is an optional dependency (``pip install warp-engine[arrow]``);
this module imports it lazily so the rest of the HTTP adapter works without it.
The IPC schema is derived from the discovered column types *before* any row
is read, so batches can be written as they arrive from the cursor.
"""

import importlib.util
from collections.abc import AsyncIterator, Callable
from typing import Any

from warp.domain.schema import ColumnSchema, TableSchema


def arrow_available() -> bool:
    """Whether ``pyarrow`` can be imported in this environment."""
    return importlib.util.find_spec("pyarrow") is not None


class ExportSerializationError(RuntimeError):
    """A value could not be represented in the announced Arrow type."""


_KIND_BY_TYPE: dict[str, str] = {
    "boolean": "bool",
    "bool": "bool",
    "bit": "bool",
    "smallint": "int16",
    "int2": "int16",
    "tinyint": "int16",
    "year": "int16",
    "integer": "int32",
    "int": "int32",
    "int4": "int32",
    "serial": "int32",
    "mediumint": "int32",
    "bigint": "int64",
    "int8": "int64",
    "bigserial": "int64",
    "numeric": "decimal",
    "decimal": "decimal",
    "real": "float32",
    "float4": "float32",
    "float": "float32",
    "double precision": "float64",
    "float8": "float64",
    "double": "float64",
    "date": "date",
    "datetime": "timestamp",
    "bytea": "binary",
    "blob": "binary",
    "tinyblob": "binary",
    "mediumblob": "binary",
    "longblob": "binary",
    "binary": "binary",
    "varbinary": "binary",
    # Oracle. A NUMBER with no declared precision has no decimal128 that can
    # hold it, so _arrow_type falls back to text for those columns.
    "number": "decimal",
    "binary_float": "float32",
    "binary_double": "float64",
    "raw": "binary",
    "long raw": "binary",
    "bfile": "binary",
}
_KIND_BY_UDT: dict[str, str] = {
    "bool": "bool",
    "int2": "int16",
    "int4": "int32",
    "int8": "int64",
    "numeric": "decimal",
    "float4": "float32",
    "float8": "float64",
    "date": "date",
    "bytea": "binary",
}


def _kind(column: ColumnSchema) -> str:
    """Coarse type family of a column; anything unknown is exported as text."""
    t = column.type.lower()
    udt = (column.udt_name or "").lower()
    full = (column.full_type or "").lower()
    if udt.startswith("_") or t == "array":
        return "array"
    if "tinyint(1)" in full:
        return "bool"
    if t.startswith("timestamp"):
        return "timestamptz" if "with time zone" in t or udt == "timestamptz" else "timestamp"
    if t.startswith("time"):
        return "time"
    return _KIND_BY_TYPE.get(t) or _KIND_BY_UDT.get(udt) or "string"


def _arrow_type(kind: str, column: ColumnSchema) -> Any:
    import pyarrow as pa

    if kind == "decimal":
        if column.precision is not None and column.scale is not None and column.precision <= 38:
            return pa.decimal128(column.precision, column.scale)
        return pa.string()
    return {
        "bool": pa.bool_(),
        "int16": pa.int16(),
        "int32": pa.int32(),
        "int64": pa.int64(),
        "float32": pa.float32(),
        "float64": pa.float64(),
        "timestamptz": pa.timestamp("us", tz="UTC"),
        "timestamp": pa.timestamp("us"),
        "date": pa.date32(),
        "time": pa.time64("us"),
        "binary": pa.binary(),
        "array": pa.string(),
        "string": pa.string(),
    }[kind]


def _coercer(kind: str) -> Callable[[Any], Any]:
    """Python-side conversion applied before ``pa.array`` for one kind."""
    import json
    from datetime import UTC, datetime
    from decimal import Decimal

    def to_text(value: Any) -> Any:
        if isinstance(value, str):
            return value
        if isinstance(value, dict | list):
            return json.dumps(value, default=str)
        return str(value)

    def to_timestamptz(value: Any) -> Any:
        if isinstance(value, datetime):
            return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
        return value

    coercers: dict[str, Callable[[Any], Any]] = {
        "bool": bool,
        "int16": int,
        "int32": int,
        "int64": int,
        "float32": float,
        "float64": float,
        "decimal": lambda v: v if isinstance(v, Decimal) else Decimal(str(v)),
        "timestamptz": to_timestamptz,
        "binary": bytes,
        "array": lambda v: json.dumps(v, default=str) if not isinstance(v, str) else v,
        "string": to_text,
    }
    return coercers.get(kind, lambda v: v)


def arrow_schema_for(columns: list[ColumnSchema]) -> Any:
    """Build the ``pa.Schema`` announced at the start of the IPC stream."""
    import pyarrow as pa

    return pa.schema([pa.field(c.name, _arrow_type(_kind(c), c), nullable=True) for c in columns])


def to_record_batch(rows: list[dict[str, Any]], schema: Any, columns: list[ColumnSchema]) -> Any:
    """Convert dict rows to a ``pa.RecordBatch`` matching ``schema``.

    Each column is coerced by kind; a value the announced type cannot hold
    falls back to text only when the field *is* text, otherwise the export
    fails with ``ExportSerializationError`` (the schema was already written).
    """
    import pyarrow as pa

    arrays = []
    for field, column in zip(schema, columns, strict=True):
        coerce = _coercer(_kind(column))
        values = [None if row.get(column.name) is None else row[column.name] for row in rows]
        try:
            arrays.append(pa.array([None if v is None else coerce(v) for v in values], field.type))
        except (pa.ArrowInvalid, pa.ArrowTypeError, TypeError, ValueError) as e:
            if field.type != pa.string():
                raise ExportSerializationError(f"Column {column.name!r}: {e}") from e
            arrays.append(pa.array([None if v is None else str(v) for v in values], pa.string()))
    return pa.RecordBatch.from_arrays(arrays, schema=schema)


async def arrow_ipc_stream(
    batches: AsyncIterator[list[dict[str, Any]]],
    table_schema: TableSchema,
    columns: list[str] | None = None,
) -> AsyncIterator[bytes]:
    """Encode row batches as an Arrow IPC stream (schema message, batches, EOS)."""
    import io

    import pyarrow as pa

    wanted = columns or table_schema.get_column_names()
    selected = [c for name in wanted if (c := table_schema.get_column(name)) is not None]
    schema = arrow_schema_for(selected)

    buffer = io.BytesIO()
    writer = pa.ipc.new_stream(buffer, schema)
    # The writer emits the schema message lazily; a zero-row batch forces it
    # out now so the client sees the schema before the first rows arrive.
    writer.write_batch(
        pa.RecordBatch.from_arrays([pa.array([], type=f.type) for f in schema], schema=schema)
    )
    yield buffer.getvalue()
    buffer.seek(0)
    buffer.truncate()
    try:
        async for batch in batches:
            if not batch:
                continue
            writer.write_batch(to_record_batch(batch, schema, selected))
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate()
    finally:
        writer.close()
    yield buffer.getvalue()
