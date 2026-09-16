"""Database column type classification.

Single source for mapping raw database type names to Python types (used to
generate request/response models) and to coarse *kinds* (used to coerce
filter values and path ids without guessing from the shape of the text).
"""

from __future__ import annotations

DB_TYPE_MAPPING: dict[str, type] = {
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
    "uuid": str,
    "json": dict,
    "jsonb": dict,
    "date": str,
    "timestamp": str,
    "timestamp with time zone": str,
    "timestamp without time zone": str,
    "time": str,
    "time with time zone": str,
    "time without time zone": str,
    "bytea": bytes,
    "array": list,
    # MySQL types
    "int": int,
    "tinyint": int,
    "mediumint": int,
    "float": float,
    "double": float,
    "bit": bool,
    "datetime": str,
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
}

# Coarse classification used for value coercion at the API boundary.
KIND_BY_PYTHON_TYPE: dict[type, str] = {
    int: "int",
    float: "float",
    bool: "bool",
    str: "str",
    dict: "json",
    bytes: "bytes",
    list: "list",
}


def python_type_for(
    db_type: str, udt_name: str | None = None, full_type: str | None = None
) -> type:
    """Map a database column type to the Python type used for API models.

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
    """Classify a column type as ``int``/``float``/``bool``/``str``/``json``/``bytes``/``list``.

    Unknown types classify as ``str`` so their values are passed through as
    text and the database performs any conversion.
    """
    return KIND_BY_PYTHON_TYPE.get(python_type_for(db_type, udt_name, full_type), "str")
