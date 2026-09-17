"""Database column type classification.

Single source for mapping raw database type names to Python types (used to
generate request/response models) and to coarse *kinds* (used to coerce
filter values and path ids without guessing from the shape of the text).
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any
from uuid import UUID

DB_TYPE_MAPPING: dict[str, Any] = {
    # PostgreSQL types
    "integer": int,
    "bigint": int,
    "smallint": int,
    "serial": int,
    "bigserial": int,
    "real": float,
    "double precision": float,
    "numeric": float,
    "decimal": float,
    "boolean": bool,
    "character varying": str,
    "varchar": str,
    "character": str,
    "char": str,
    "text": str,
    "uuid": UUID,
    "json": Any,
    "jsonb": Any,
    "date": date,
    "timestamp": datetime,
    "timestamp with time zone": datetime,
    "timestamp without time zone": datetime,
    "time": time,
    "time with time zone": time,
    "time without time zone": time,
    "bytea": bytes,
    "array": list,
    # MySQL types
    "int": int,
    "tinyint": int,
    "mediumint": int,
    "float": float,
    "double": float,
    "bit": bool,
    "datetime": datetime,
    "year": int,
    "enum": str,
    "set": str,
    "blob": bytes,
    "tinyblob": bytes,
    "mediumblob": bytes,
    "longblob": bytes,
    "tinytext": str,
    "mediumtext": str,
    "longtext": str,
    # SQL Server types (as reported by the ODBC adapter)
    "nvarchar": str,
    "nchar": str,
    "ntext": str,
    "sysname": str,
    "xml": str,
    "sql_variant": str,
    "datetime2": datetime,
    "smalldatetime": datetime,
    "datetimeoffset": datetime,
    "money": float,
    "smallmoney": float,
    "uniqueidentifier": UUID,
    "varbinary": bytes,
    "binary": bytes,
    "image": bytes,
    "rowversion": bytes,
    "hierarchyid": bytes,
    "geography": bytes,
    "geometry": bytes,
    # Other ODBC data sources (Oracle and friends)
    "number": float,
    "varchar2": str,
    "nvarchar2": str,
    "clob": str,
    "nclob": str,
    "long": str,
    "raw": bytes,
}

# Coarse classification used for value coercion at the API boundary.
KIND_BY_PYTHON_TYPE: dict[Any, str] = {
    int: "int",
    float: "float",
    bool: "bool",
    str: "str",
    Any: "json",
    bytes: "bytes",
    list: "list",
    datetime: "datetime",
    date: "date",
    time: "time",
    UUID: "uuid",
}


def python_type_for(db_type: str, udt_name: str | None = None, full_type: str | None = None) -> Any:
    """Map a database column type to the Python type used for API models.

    Temporal columns map to `datetime`/`date`/`time`, `uuid` to `UUID` and JSON
    to `Any`, because that is what the drivers hand back for real rows (a
    `str` field would reject a `datetime` value at response validation).

    Args:
        db_type: Data type as reported by the database (``integer``, ``varchar``...).
        udt_name: PostgreSQL underlying type name (``int4``, ``_text`` for arrays).
        full_type: MySQL full column type (``tinyint(1)`` is a boolean).

    Returns:
        A Python type; ``str`` when the type is unknown.
    """
    # MySQL has no native boolean: BOOLEAN columns are reported as tinyint(1).
    if full_type and "tinyint(1)" in full_type.lower():
        return bool

    lowered = db_type.lower()
    if lowered in DB_TYPE_MAPPING:
        return DB_TYPE_MAPPING[lowered]

    if udt_name:
        udt = udt_name.lower()
        if udt in DB_TYPE_MAPPING:
            return DB_TYPE_MAPPING[udt]
        if udt.startswith("_"):  # PostgreSQL array types
            return list

    return str


def type_kind(db_type: str, udt_name: str | None = None, full_type: str | None = None) -> str:
    """Classify a column type: int/float/bool/str/json/bytes/list/datetime/date/time/uuid.

    Unknown types classify as ``str`` so their values are passed through as
    text and the database performs any conversion.
    """
    return KIND_BY_PYTHON_TYPE.get(python_type_for(db_type, udt_name, full_type), "str")
