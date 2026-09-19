"""ODBC database adapter: SQL Server first, any other ODBC data source best-effort.

Runs on aioodbc/pyodbc (the ``odbc`` extra); the ODBC *driver* itself, e.g.
Microsoft's ``msodbcsql18``, is a system package.

Profiles (selected by the config ``type``):

- ``mssql`` / ``sqlserver``: full introspection through ``INFORMATION_SCHEMA``
  and ``sys.*`` (identity and computed columns, primary/foreign keys, indexes),
  writes that hand the row back with ``OUTPUT`` (falling back to a re-select on
  tables with triggers, SQL Server error 334), ``TOP`` / ``OFFSET ... FETCH``
  pagination and a ``datetimeoffset`` output converter.
- ``odbc``: ANSI quoting, ``?`` placeholders, introspection through the ODBC
  catalog functions (SQLTables/SQLColumns/SQLPrimaryKeys/SQLForeignKeys/
  SQLStatistics) and re-selects after writes. Identity detection depends on
  what the driver reports in ``TYPE_NAME``.

``aioodbc`` is imported lazily in :meth:`ODBCAdapter.connect` so the factory
(and the unit tests) work without the extra installed.
"""

from __future__ import annotations

import logging
import struct
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from warp.adapters.outbound.db.base import DatabaseAdapter
from warp.adapters.outbound.db.dialect import Dialect, get_dialect
from warp.adapters.outbound.db.identifiers import sanitize_identifier
from warp.adapters.outbound.db.params import bind_named_params
from warp.adapters.outbound.db.query_builder import SafeQueryBuilder

logger = logging.getLogger(__name__)

DEFAULT_MSSQL_DRIVER = "ODBC Driver 18 for SQL Server"

# SQL Server's DATETIMEOFFSET has no native pyodbc mapping (SQL type -155).
SQL_SS_TIMESTAMPOFFSET = -155

# --- connection string -----------------------------------------------------------


def _odbc_value(value: Any) -> str:
    """Brace-quote a connection-string value when ODBC rules require it."""
    text = str(value)
    if any(ch in text for ch in ";{}") or text != text.strip():
        return "{" + text.replace("}", "}}") + "}"
    return text


def _yes_no(value: Any) -> str:
    """Render a boolean option as ``yes``/``no``; pass driver keywords through."""
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def build_connection_string(config: Mapping[str, Any]) -> str:
    """Build the ODBC connection string for a database config.

    ``options.connection_string`` is used verbatim (``UID``/``PWD`` from
    ``username``/``password`` are appended when it carries no ``UID``, so the
    read-only credentials of the raw SQL endpoint still apply). Otherwise the
    SQL Server profile assembles ``DRIVER``/``SERVER``/``DATABASE``/``UID``/
    ``PWD``/``Encrypt``/``TrustServerCertificate``, Oracle assembles
    ``DRIVER``/``DBQ``/``UID``/``PWD``, and the generic profile requires
    ``options.driver``. ``options.extra`` is appended as raw ``Key=Value;``
    pairs.

    Oracle is the odd one out: its driver takes an Easy Connect descriptor in
    ``DBQ`` (``host:port/service``) and has no ``SERVER``/``DATABASE`` pair, so
    the SQL Server spelling reaches it as ORA-12162.

    Raises:
        ValueError: For a generic ODBC config without ``driver`` or
            ``connection_string``.
    """
    options: Mapping[str, Any] = config.get("options") or {}
    db_type = str(config.get("type", "odbc")).lower()
    is_mssql = db_type in ("mssql", "sqlserver")
    is_oracle = db_type == "oracle"
    parts: list[str] = []

    verbatim = options.get("connection_string")
    if verbatim:
        base = str(verbatim).strip().rstrip(";")
        parts.append(base)
        if "uid=" not in base.lower() and config.get("username"):
            parts.append(f"UID={_odbc_value(config['username'])}")
            parts.append(f"PWD={_odbc_value(config.get('password') or '')}")
    else:
        driver = options.get("driver") or (DEFAULT_MSSQL_DRIVER if is_mssql else None)
        if not driver:
            raise ValueError(
                "ODBC connections need options.driver (the ODBC driver name) "
                "or options.connection_string"
            )
        parts.append(f"DRIVER={{{str(driver).strip('{}')}}}")
        host = config.get("host") or "localhost"
        port = config.get("port")
        if is_oracle:
            # Easy Connect: host:port/service_name. `database` is the service.
            target = f"{host}:{port}" if port else str(host)
            service = config.get("database")
            parts.append(f"DBQ={_odbc_value(f'{target}/{service}' if service else target)}")
        else:
            parts.append(f"SERVER={_odbc_value(f'{host},{port}' if port else host)}")
            if config.get("database"):
                parts.append(f"DATABASE={_odbc_value(config['database'])}")
        if config.get("username"):
            parts.append(f"UID={_odbc_value(config['username'])}")
            parts.append(f"PWD={_odbc_value(config.get('password') or '')}")
        if is_mssql:
            parts.append(f"Encrypt={_yes_no(options.get('encrypt', True))}")
            trust = options.get("trust_server_certificate", False)
            parts.append(f"TrustServerCertificate={_yes_no(trust)}")

    extra = options.get("extra")
    if extra:
        parts.append(str(extra).strip().rstrip(";"))
    return ";".join(parts) + ";"


# --- driver quirks -------------------------------------------------------------------


def decode_datetimeoffset(raw: bytes) -> datetime:
    """Decode SQL Server's ``SQL_SS_TIMESTAMPOFFSET_STRUCT`` into an aware datetime."""
    year, month, day, hour, minute, second, nanoseconds, tz_hours, tz_minutes = struct.unpack(
        "<6hI2h", raw
    )
    tz = timezone(timedelta(hours=tz_hours, minutes=tz_minutes))
    return datetime(year, month, day, hour, minute, second, nanoseconds // 1000, tzinfo=tz)


async def configure_mssql_connection(conn: Any) -> None:
    """``after_created`` hook: make ``datetimeoffset`` columns readable."""
    conn.add_output_converter(SQL_SS_TIMESTAMPOFFSET, decode_datetimeoffset)


def _is_output_with_trigger_error(error: BaseException) -> bool:
    """SQL Server error 334: OUTPUT without INTO on a table that has triggers."""
    message = str(error)
    return "(334)" in message or "output clause without into" in message.lower()


def _strip_default(value: Any) -> Any:
    """Unwrap SQL Server's ``((1))`` / ``(getdate())`` default-expression parentheses."""
    if not isinstance(value, str):
        return value
    text = value.strip()
    while text.startswith("(") and text.endswith(")") and _outer_parens_match(text):
        text = text[1:-1].strip()
    return text


def _outer_parens_match(text: str) -> bool:
    depth = 0
    for index, char in enumerate(text):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index == len(text) - 1
    return False


def _mssql_full_type(
    data_type: str, max_length: int | None, precision: int | None, scale: int | None
) -> str:
    if data_type in {"varchar", "nvarchar", "char", "nchar", "varbinary", "binary"}:
        if max_length == -1:
            return f"{data_type}(max)"
        if max_length is not None:
            return f"{data_type}({max_length})"
    if data_type in {"decimal", "numeric"} and precision is not None:
        return f"{data_type}({precision},{scale or 0})"
    return data_type


def _as_int(value: Any) -> int | None:
    """Oracle reports catalog numbers as NUMBER, which pyodbc hands back as float.

    Lengths, precisions and scales are whole numbers everywhere else and are
    typed that way downstream (``pa.decimal128`` will not take a float), so
    they are narrowed here rather than leaking ``varchar2(50.0)`` into a type
    name.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _oracle_full_type(
    data_type: str, max_length: int | None, precision: int | None, scale: int | None
) -> str:
    """Render an Oracle type with its length or precision.

    A ``NUMBER`` with no declared precision is Oracle's arbitrary-precision
    type; it stays bare, which is what tells the Arrow exporter it has no
    ``decimal128`` mapping.
    """
    if data_type in {"varchar2", "nvarchar2", "char", "nchar", "raw"} and max_length:
        return f"{data_type}({max_length})"
    if data_type == "number" and precision is not None:
        return f"{data_type}({precision},{scale or 0})"
    return data_type


# --- SQL Server introspection --------------------------------------------------------

_MSSQL_TABLES_SQL = """
SELECT TABLE_NAME AS table_name
FROM INFORMATION_SCHEMA.TABLES
WHERE TABLE_SCHEMA = ? AND TABLE_TYPE = 'BASE TABLE'
ORDER BY TABLE_NAME
"""

_MSSQL_COLUMNS_SQL = """
SELECT
    c.COLUMN_NAME AS column_name,
    c.DATA_TYPE AS data_type,
    c.IS_NULLABLE AS is_nullable,
    c.COLUMN_DEFAULT AS column_default,
    c.CHARACTER_MAXIMUM_LENGTH AS max_length,
    c.NUMERIC_PRECISION AS numeric_precision,
    c.NUMERIC_SCALE AS numeric_scale,
    COLUMNPROPERTY(OBJECT_ID(QUOTENAME(c.TABLE_SCHEMA) + '.' + QUOTENAME(c.TABLE_NAME)),
                   c.COLUMN_NAME, 'IsIdentity') AS is_identity,
    COLUMNPROPERTY(OBJECT_ID(QUOTENAME(c.TABLE_SCHEMA) + '.' + QUOTENAME(c.TABLE_NAME)),
                   c.COLUMN_NAME, 'IsComputed') AS is_computed
FROM INFORMATION_SCHEMA.COLUMNS c
WHERE c.TABLE_SCHEMA = ? AND c.TABLE_NAME = ?
ORDER BY c.ORDINAL_POSITION
"""

_MSSQL_PRIMARY_KEY_SQL = """
SELECT kcu.COLUMN_NAME AS column_name
FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
  ON kcu.CONSTRAINT_NAME = tc.CONSTRAINT_NAME
 AND kcu.CONSTRAINT_SCHEMA = tc.CONSTRAINT_SCHEMA
 AND kcu.TABLE_NAME = tc.TABLE_NAME
WHERE tc.CONSTRAINT_TYPE = 'PRIMARY KEY'
  AND tc.TABLE_SCHEMA = ? AND tc.TABLE_NAME = ?
ORDER BY kcu.ORDINAL_POSITION
"""

_MSSQL_FOREIGN_KEYS_SQL = """
SELECT
    pc.name AS column_name,
    rt.name AS foreign_table,
    rc.name AS foreign_column,
    fk.name AS constraint_name
FROM sys.foreign_keys fk
JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id
JOIN sys.tables t ON t.object_id = fk.parent_object_id
JOIN sys.schemas s ON s.schema_id = t.schema_id
JOIN sys.columns pc ON pc.object_id = fkc.parent_object_id AND pc.column_id = fkc.parent_column_id
JOIN sys.tables rt ON rt.object_id = fk.referenced_object_id
JOIN sys.columns rc
  ON rc.object_id = fkc.referenced_object_id AND rc.column_id = fkc.referenced_column_id
WHERE s.name = ? AND t.name = ?
ORDER BY fk.name, fkc.constraint_column_id
"""

_ORACLE_TABLES_SQL = """
SELECT TABLE_NAME AS "table_name"
FROM ALL_TABLES
WHERE OWNER = ?
ORDER BY TABLE_NAME
"""

# IDENTITY_COLUMN and DEFAULT_ON_NULL arrived in 12c; older servers raise
# ORA-00904 for them, which _oracle_columns turns into the 11g fallback below.
_ORACLE_COLUMNS_SQL = """
SELECT
    COLUMN_NAME AS "column_name",
    DATA_TYPE AS "data_type",
    NULLABLE AS "nullable",
    DATA_DEFAULT AS "column_default",
    CHAR_LENGTH AS "max_length",
    DATA_PRECISION AS "numeric_precision",
    DATA_SCALE AS "numeric_scale",
    IDENTITY_COLUMN AS "is_identity",
    VIRTUAL_COLUMN AS "is_virtual"
FROM ALL_TAB_COLS
WHERE OWNER = ? AND TABLE_NAME = ? AND HIDDEN_COLUMN = 'NO'
ORDER BY COLUMN_ID
"""

_ORACLE_COLUMNS_LEGACY_SQL = """
SELECT
    COLUMN_NAME AS "column_name",
    DATA_TYPE AS "data_type",
    NULLABLE AS "nullable",
    DATA_DEFAULT AS "column_default",
    CHAR_LENGTH AS "max_length",
    DATA_PRECISION AS "numeric_precision",
    DATA_SCALE AS "numeric_scale",
    'NO' AS "is_identity",
    'NO' AS "is_virtual"
FROM ALL_TAB_COLUMNS
WHERE OWNER = ? AND TABLE_NAME = ?
ORDER BY COLUMN_ID
"""

_ORACLE_PRIMARY_KEY_SQL = """
SELECT acc.COLUMN_NAME AS "column_name"
FROM ALL_CONSTRAINTS ac
JOIN ALL_CONS_COLUMNS acc
  ON acc.OWNER = ac.OWNER AND acc.CONSTRAINT_NAME = ac.CONSTRAINT_NAME
WHERE ac.CONSTRAINT_TYPE = 'P' AND ac.OWNER = ? AND ac.TABLE_NAME = ?
ORDER BY acc.POSITION
"""

# Oracle foreign keys point at the *constraint* they reference (R_CONSTRAINT_NAME),
# so the referenced table/column come from a second hop through ALL_CONS_COLUMNS.
_ORACLE_FOREIGN_KEYS_SQL = """
SELECT
    acc.COLUMN_NAME AS "column_name",
    rcc.TABLE_NAME AS "foreign_table",
    rcc.COLUMN_NAME AS "foreign_column",
    ac.CONSTRAINT_NAME AS "constraint_name"
FROM ALL_CONSTRAINTS ac
JOIN ALL_CONS_COLUMNS acc
  ON acc.OWNER = ac.OWNER AND acc.CONSTRAINT_NAME = ac.CONSTRAINT_NAME
JOIN ALL_CONS_COLUMNS rcc
  ON rcc.OWNER = ac.R_OWNER AND rcc.CONSTRAINT_NAME = ac.R_CONSTRAINT_NAME
 AND rcc.POSITION = acc.POSITION
WHERE ac.CONSTRAINT_TYPE = 'R' AND ac.OWNER = ? AND ac.TABLE_NAME = ?
ORDER BY ac.CONSTRAINT_NAME, acc.POSITION
"""

_ORACLE_INDEXES_SQL = """
SELECT ai.INDEX_NAME AS "index_name", aic.COLUMN_NAME AS "column_name", ai.UNIQUENESS AS "uniqueness"
FROM ALL_INDEXES ai
JOIN ALL_IND_COLUMNS aic
  ON aic.INDEX_OWNER = ai.OWNER AND aic.INDEX_NAME = ai.INDEX_NAME
WHERE ai.TABLE_OWNER = ? AND ai.TABLE_NAME = ?
  AND NOT EXISTS (
      SELECT 1 FROM ALL_CONSTRAINTS ac
      WHERE ac.OWNER = ai.OWNER AND ac.CONSTRAINT_NAME = ai.INDEX_NAME
        AND ac.CONSTRAINT_TYPE = 'P'
  )
ORDER BY ai.INDEX_NAME, aic.COLUMN_POSITION
"""

_ORACLE_IDENTITY_SQL = """
SELECT COLUMN_NAME AS "column_name", SEQUENCE_NAME AS "sequence_name"
FROM ALL_TAB_IDENTITY_COLS
WHERE OWNER = ? AND TABLE_NAME = ?
"""

_ORACLE_ROW_ESTIMATES_SQL = """
SELECT TABLE_NAME AS "table_name", NUM_ROWS AS "row_count"
FROM ALL_TABLES
WHERE OWNER = ? AND TABLE_NAME IN ({placeholders})
"""

_MSSQL_INDEXES_SQL = """
SELECT i.name AS index_name, c.name AS column_name, i.is_unique AS is_unique
FROM sys.indexes i
JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
JOIN sys.tables t ON t.object_id = i.object_id
JOIN sys.schemas s ON s.schema_id = t.schema_id
WHERE s.name = ? AND t.name = ?
  AND i.is_primary_key = 0 AND i.type > 0 AND ic.is_included_column = 0
ORDER BY i.name, ic.key_ordinal
"""


class ODBCAdapter(DatabaseAdapter):
    """Database adapter for ODBC data sources (SQL Server profile + generic profile)."""

    def __init__(self, config: dict[str, Any]):
        """Resolve the dialect profile and the introspection schema from the config."""
        super().__init__(config)
        self.dialect: Dialect = get_dialect(str(config.get("type") or "odbc"))
        self._qb = SafeQueryBuilder(self.dialect)
        self._mssql = self.dialect.name == "mssql"
        self._oracle = self.dialect.name == "oracle"
        self._schema = self.dialect.default_schema(
            str(config.get("database") or ""), config.get("options") or {}
        )
        # Oracle's schema is the connecting user, not the database (which is a
        # service name). Resolved from the session in connect() unless the
        # config named one explicitly.
        self._resolve_schema = self._oracle and not (config.get("options") or {}).get("schema")
        # Tables where OUTPUT failed with error 334 (triggers): use re-selects.
        self._output_disabled: set[str] = set()
        self._pk_cache: dict[str, str | None] = {}
        self._identity_sequences: dict[str, str | None] = {}
        self._warned_inexact: set[str] = set()

    # --- lifecycle ----------------------------------------------------------

    async def connect(self) -> None:
        """Create the aioodbc connection pool."""
        try:
            import aioodbc
        except ImportError as e:
            raise RuntimeError(
                "ODBC support needs the 'odbc' extra: pip install 'warp-engine[odbc]' "
                "(plus an ODBC driver such as msodbcsql18)"
            ) from e

        options: Mapping[str, Any] = self.config.get("options") or {}
        self._pool = await aioodbc.create_pool(
            dsn=build_connection_string(self.config),
            minsize=int(options.get("pool_min_size", 2)),
            maxsize=int(options.get("pool_size", 10)),
            pool_recycle=int(options.get("pool_recycle", -1)),
            autocommit=True,
            after_created=configure_mssql_connection if self._mssql else None,
        )
        if self._resolve_schema:
            await self._adopt_session_schema()

    async def _adopt_session_schema(self) -> None:
        """Use the connected Oracle user as the schema to introspect.

        Oracle's `database` is a service name, so the generic "database name is
        the schema" fallback is wrong here. Degrades to whatever was configured
        rather than failing the connection.
        """
        try:
            rows, _ = await self._execute("SELECT USER AS username FROM DUAL")
        except Exception as e:  # pragma: no cover - driver-specific
            logger.warning(f"Could not resolve the Oracle session schema: {e}")
            return
        if rows:
            self._schema = str(next(iter(rows[0].values()))).upper()
            logger.debug(f"Oracle schema resolved from the session: {self._schema}")

    async def disconnect(self) -> None:
        """Close the connection pool."""
        if self._pool:
            self._pool.close()
            await self._pool.wait_closed()
            self._pool = None

    # --- execution helpers --------------------------------------------------

    @staticmethod
    async def _fetch_dicts(cur: Any) -> list[dict[str, Any]]:
        """Rows of the current result set as dicts (``[]`` when there is none)."""
        if cur.description is None:
            return []
        names = [column[0] for column in cur.description]
        rows = await cur.fetchall()
        return [dict(zip(names, row, strict=False)) for row in rows]

    async def _execute(
        self, sql: str, args: Sequence[Any] = ()
    ) -> tuple[list[dict[str, Any]], int]:
        """Run one statement; return its rows (if any) and ``rowcount``."""
        async with self._pool.acquire() as conn, conn.cursor() as cur:
            if args:
                await cur.execute(sql, list(args))
            else:
                await cur.execute(sql)
            rows = await self._fetch_dicts(cur)
            return rows, int(cur.rowcount)

    # --- introspection ------------------------------------------------------

    async def get_tables(self) -> list[str]:
        """Base tables of the configured schema."""
        if self._mssql:
            rows, _ = await self._execute(_MSSQL_TABLES_SQL, [self._schema])
            return [str(row["table_name"]) for row in rows]
        if self._oracle:
            rows, _ = await self._execute(_ORACLE_TABLES_SQL, [self._schema])
            return [str(row["table_name"]) for row in rows]
        async with self._pool.acquire() as conn, conn.cursor() as cur:
            await cur.tables(schema=self._schema or None, tableType="TABLE")
            rows = await cur.fetchall()
        return sorted(str(row[2]) for row in rows)

    async def get_table_schema(self, table: str) -> dict[str, Any]:
        """Columns, primary key, foreign keys and indexes of a table."""
        if self._mssql:
            return await self._mssql_table_schema(table)
        if self._oracle:
            return await self._oracle_table_schema(table)
        return await self._generic_table_schema(table)

    async def _oracle_table_schema(self, table: str) -> dict[str, Any]:
        scope = [self._schema, self.dialect.fold_identifier(table)]
        columns = await self._oracle_columns(scope)
        pk_rows, _ = await self._execute(_ORACLE_PRIMARY_KEY_SQL, scope)
        fk_rows, _ = await self._execute(_ORACLE_FOREIGN_KEYS_SQL, scope)
        idx_rows, _ = await self._execute(_ORACLE_INDEXES_SQL, scope)

        pk_columns = [str(row["column_name"]) for row in pk_rows]
        built = [self._oracle_column(col, col["column_name"] in pk_columns) for col in columns]
        self._warn_about_inexact_numbers(table, built)
        return {
            "table_name": table,
            "columns": built,
            "primary_key": self._primary_key_value(pk_columns),
            "foreign_keys": [
                {
                    "column": fk["column_name"],
                    "references_table": fk["foreign_table"],
                    "references_column": fk["foreign_column"],
                    "constraint_name": fk["constraint_name"],
                }
                for fk in fk_rows
            ],
            "indexes": self._group_indexes(
                (
                    str(row["index_name"]),
                    str(row["column_name"]),
                    str(row["uniqueness"]).upper() == "UNIQUE",
                )
                for row in idx_rows
            ),
        }

    def _warn_about_inexact_numbers(self, table: str, columns: list[dict[str, Any]]) -> None:
        """Say once, per table, which columns cannot round-trip exactly.

        An Oracle ``NUMBER`` with no declared precision arrives as an IEEE
        double, so an id past 2**53 comes back rounded and there is no later
        point at which the real value could be recovered. Declaring the
        precision (``NUMBER(38)``) makes the driver hand over an exact
        ``Decimal`` instead, which is why this names the fix rather than just
        reporting the problem.
        """
        lossy = [str(col["name"]) for col in columns if col.get("inexact")]
        if not lossy or table in self._warned_inexact:
            return
        self._warned_inexact.add(table)
        logger.warning(
            "%s.%s: %s declared as NUMBER without a precision. The ODBC driver "
            "returns these as floating point, so values above 2^53 lose "
            "precision before Warp sees them. Declare a precision "
            "(e.g. NUMBER(38)) to get exact values.",
            self._schema,
            table,
            ", ".join(lossy),
        )

    async def _oracle_columns(self, scope: Sequence[Any]) -> list[dict[str, Any]]:
        """Columns of a table, falling back to the pre-12c catalog view."""
        try:
            rows, _ = await self._execute(_ORACLE_COLUMNS_SQL, scope)
            return rows
        except Exception as e:
            if "ORA-00904" not in str(e):
                raise
            logger.debug("Oracle identity columns unavailable (pre-12c); using ALL_TAB_COLUMNS")
            rows, _ = await self._execute(_ORACLE_COLUMNS_LEGACY_SQL, scope)
            return rows

    @staticmethod
    def _oracle_column(col: Mapping[str, Any], is_pk: bool) -> dict[str, Any]:
        data_type = str(col["data_type"]).lower()
        extra = None
        if str(col.get("is_identity") or "NO").upper() == "YES":
            extra = "identity"
        elif str(col.get("is_virtual") or "NO").upper() == "YES":
            extra = "computed"
        precision = _as_int(col.get("numeric_precision"))
        scale = _as_int(col.get("numeric_scale"))
        max_length = _as_int(col.get("max_length"))
        lossy = data_type == "number" and precision is None
        return {
            "name": col["column_name"],
            "type": data_type,
            "full_type": _oracle_full_type(data_type, max_length, precision, scale),
            # Oracle spells nullability 'Y'/'N', and has no separate empty string:
            # '' IS NULL is true, so a NOT NULL varchar2 really is non-empty.
            "nullable": str(col["nullable"]).upper() == "Y",
            "default": _strip_default(col.get("column_default")),
            "max_length": max_length,
            "precision": precision,
            "scale": scale,
            "key": "PRI" if is_pk else None,
            "extra": extra,
            # An undeclared NUMBER is handed over as an IEEE double by the
            # driver, so values past 2**53 are already rounded before Python
            # ever sees them. Nothing downstream can undo that, so the column
            # is flagged and the adapter says so once, at startup.
            "inexact": lossy,
        }

    async def _mssql_table_schema(self, table: str) -> dict[str, Any]:
        scope = [self._schema, table]
        columns, _ = await self._execute(_MSSQL_COLUMNS_SQL, scope)
        pk_rows, _ = await self._execute(_MSSQL_PRIMARY_KEY_SQL, scope)
        fk_rows, _ = await self._execute(_MSSQL_FOREIGN_KEYS_SQL, scope)
        idx_rows, _ = await self._execute(_MSSQL_INDEXES_SQL, scope)

        pk_columns = [str(row["column_name"]) for row in pk_rows]
        schema: dict[str, Any] = {
            "table_name": table,
            "columns": [
                self._mssql_column(col, col["column_name"] in pk_columns) for col in columns
            ],
            "primary_key": self._primary_key_value(pk_columns),
            "foreign_keys": [
                {
                    "column": fk["column_name"],
                    "references_table": fk["foreign_table"],
                    "references_column": fk["foreign_column"],
                    "constraint_name": fk["constraint_name"],
                }
                for fk in fk_rows
            ],
            "indexes": self._group_indexes(
                (str(row["index_name"]), str(row["column_name"]), bool(row["is_unique"]))
                for row in idx_rows
            ),
        }
        return schema

    @staticmethod
    def _mssql_column(col: Mapping[str, Any], is_pk: bool) -> dict[str, Any]:
        data_type = str(col["data_type"]).lower()
        # SQL Server's legacy `timestamp` is an 8-byte row version, not a time.
        if data_type == "timestamp":
            data_type = "rowversion"
        extra = None
        if col.get("is_identity") == 1:
            extra = "identity"
        elif col.get("is_computed") == 1:
            extra = "computed"
        return {
            "name": col["column_name"],
            "type": data_type,
            "full_type": _mssql_full_type(
                data_type,
                col.get("max_length"),
                col.get("numeric_precision"),
                col.get("numeric_scale"),
            ),
            "nullable": col["is_nullable"] == "YES",
            "default": _strip_default(col.get("column_default")),
            "max_length": col.get("max_length"),
            "precision": col.get("numeric_precision"),
            "scale": col.get("numeric_scale"),
            "key": "PRI" if is_pk else None,
            "extra": extra,
        }

    async def _generic_table_schema(self, table: str) -> dict[str, Any]:
        schema_arg = self._schema or None
        async with self._pool.acquire() as conn, conn.cursor() as cur:
            await cur.columns(table=table, schema=schema_arg)
            column_rows = await cur.fetchall()
            await cur.primaryKeys(table, schema=schema_arg)
            pk_rows = await cur.fetchall()
            await cur.foreignKeys(foreignTable=table, foreignSchema=schema_arg)
            fk_rows = await cur.fetchall()
            idx_rows = await self._generic_statistics(cur, table, schema_arg)

        pk_columns = [str(row[3]) for row in sorted(pk_rows, key=lambda row: row[4] or 0)]
        return {
            "table_name": table,
            "columns": [
                self._generic_column(row, str(row[3]) in pk_columns) for row in column_rows
            ],
            "primary_key": self._primary_key_value(pk_columns),
            "foreign_keys": [
                {
                    "column": row[7],
                    "references_table": row[2],
                    "references_column": row[3],
                    "constraint_name": row[11],
                }
                for row in fk_rows
            ],
            "indexes": self._group_indexes(
                (str(row[5]), str(row[8]), not row[3])
                for row in sorted(idx_rows, key=lambda row: (str(row[5]), row[7] or 0))
                if row[5] is not None and row[8] is not None
            ),
        }

    @staticmethod
    async def _generic_statistics(cur: Any, table: str, schema: str | None) -> list[Any]:
        """SQLStatistics rows, or ``[]`` when the driver/wrapper cannot provide them."""
        try:
            try:
                await cur.statistics(table, schema=schema)
            except TypeError:
                # aioodbc 0.5's wrapper drops pyodbc's mandatory `table` argument.
                await cur._run_operation(cur._impl.statistics, table, schema=schema)
            rows: list[Any] = await cur.fetchall()
            return rows
        except Exception as e:
            logger.debug(f"Index statistics unavailable for {table}: {e}")
            return []

    @staticmethod
    def _generic_column(row: Sequence[Any], is_pk: bool) -> dict[str, Any]:
        """Map a SQLColumns row (ODBC column order) to warp's column dict."""
        type_name = str(row[5] or "").lower()
        base_type = type_name.split("(")[0].strip()
        extra = None
        if "identity" in base_type:  # e.g. SQL Server reports "int identity"
            extra = "identity"
            base_type = base_type.replace("identity", "").strip()
        is_nullable = row[17] if len(row) > 17 else None
        nullable = str(is_nullable).upper() == "YES" if is_nullable else bool(row[10])
        size = row[6]
        is_text = any(token in base_type for token in ("char", "text", "binary", "clob", "raw"))
        is_numeric = any(token in base_type for token in ("dec", "num", "money"))
        return {
            "name": row[3],
            "type": base_type,
            "full_type": type_name or None,
            "nullable": nullable,
            "default": row[12] if len(row) > 12 else None,
            "max_length": size if is_text else None,
            "precision": size if is_numeric else None,
            "scale": row[8] if is_numeric else None,
            "key": "PRI" if is_pk else None,
            "extra": extra,
        }

    @staticmethod
    def _primary_key_value(pk_columns: list[str]) -> str | list[str] | None:
        if not pk_columns:
            return None
        return pk_columns[0] if len(pk_columns) == 1 else pk_columns

    @staticmethod
    def _group_indexes(entries: Any) -> list[dict[str, Any]]:
        idx_map: dict[str, dict[str, Any]] = {}
        for name, column, unique in entries:
            if name not in idx_map:
                idx_map[name] = {"name": name, "columns": [], "unique": unique}
            idx_map[name]["columns"].append(column)
        return list(idx_map.values())

    async def _primary_key(self, table: str) -> str | None:
        """First primary-key column of ``table`` (cached), or ``None``."""
        if table not in self._pk_cache:
            pk = (await self.get_table_schema(table)).get("primary_key")
            if isinstance(pk, list):
                pk = pk[0] if pk else None
            self._pk_cache[table] = pk if isinstance(pk, str) else None
        return self._pk_cache[table]

    # --- raw SQL --------------------------------------------------------------

    async def execute_query(
        self, query: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Execute a raw SQL query with ``:name`` parameters."""
        sql, args = bind_named_params(query, params, self.dialect)
        rows, _ = await self._execute(sql, args)
        return rows

    # --- CRUD -----------------------------------------------------------------

    def _use_output(self, table: str) -> bool:
        return self._mssql and table not in self._output_disabled

    def _disable_output(self, table: str, error: BaseException) -> None:
        logger.info(
            f"OUTPUT clause rejected on {table} (triggers present); "
            f"re-selecting written rows instead: {error}"
        )
        self._output_disabled.add(table)

    async def insert(self, table: str, data: dict[str, Any]) -> dict[str, Any]:
        """Insert a record and return it (via OUTPUT, or a re-select)."""
        if self._use_output(table):
            sql, values = self._qb.build_insert(table, data)
            try:
                rows, _ = await self._execute(sql, values)
            except Exception as e:
                if not _is_output_with_trigger_error(e):
                    raise
                self._disable_output(table, e)
            else:
                return rows[0] if rows else dict(data)

        sql, values = self._qb.build_insert(table, data, returning=False)
        new_id: Any = None
        if self._oracle:
            new_id = await self._oracle_insert(table, sql, values)
        elif self._mssql:
            # NOCOUNT keeps the INSERT from producing a result set, so the only
            # one is the SELECT; it is switched back so later rowcounts work.
            batch = (
                f"SET NOCOUNT ON; {sql}; "
                "SELECT CAST(SCOPE_IDENTITY() AS bigint) AS new_id; SET NOCOUNT OFF;"
            )
            rows, _ = await self._execute(batch, values)
            new_id = rows[0].get("new_id") if rows else None
        else:
            await self._execute(sql, values)

        pk = await self._primary_key(table)
        key = new_id if new_id is not None else (data.get(pk) if pk else None)
        if pk is None or key is None:
            return dict(data)
        row = await self.select_by_id(table, pk, key)
        return row if row is not None else dict(data)

    async def _oracle_insert(self, table: str, sql: str, values: Sequence[Any]) -> Any:
        """Insert, then read the identity column's new value.

        Oracle has no ``RETURNING`` this gateway can consume — it binds output
        parameters — so the generated key comes from the identity column's
        sequence instead. ``CURRVAL`` is session state, so the INSERT and the
        lookup must run on **one** pooled connection; splitting them across two
        would read another session's value or raise ORA-08002.

        Returns:
            The new key, or ``None`` when the table has no identity column (the
            caller then falls back to a key supplied in the data).
        """
        sequence = await self._oracle_identity_sequence(table)
        async with self._pool.acquire() as conn, conn.cursor() as cur:
            await cur.execute(sql, list(values))
            if sequence is None:
                return None
            await cur.execute(f"SELECT {self.dialect.quote(sequence)}.CURRVAL FROM DUAL")
            row = await cur.fetchone()
        return row[0] if row else None

    async def _oracle_identity_sequence(self, table: str) -> str | None:
        """The sequence backing ``table``'s identity column, if it has one."""
        folded = self.dialect.fold_identifier(table)
        if folded in self._identity_sequences:
            return self._identity_sequences[folded]
        sequence: str | None = None
        try:
            rows, _ = await self._execute(_ORACLE_IDENTITY_SQL, [self._schema, folded])
            if rows:
                sequence = str(rows[0]["sequence_name"])
        except Exception as e:  # pragma: no cover - pre-12c has no such view
            logger.debug("No identity metadata for %s: %s", table, e)
        self._identity_sequences[folded] = sequence
        return sequence

    async def select(
        self,
        table: str,
        columns: list[str] | None = None,
        filters: list[tuple[str, str, Any]] | None = None,
        pagination: dict[str, int] | None = None,
        sort: list[tuple[str, str]] | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Select records with filtering, pagination, and sorting."""
        count_sql, sql, params = self._qb.build_select(table, columns, filters, pagination, sort)
        count_rows, _ = await self._execute(count_sql, params)
        # Some drivers upper-case result names; the count is the only column.
        total = int(next(iter(count_rows[0].values()))) if count_rows else 0
        rows, _ = await self._execute(sql, params)
        return rows, total

    async def stream_select(  # noqa: PLR0913
        self,
        table: str,
        columns: list[str] | None = None,
        filters: list[tuple[str, str, Any]] | None = None,
        sort: list[tuple[str, str]] | None = None,
        batch_size: int = 5000,
        limit: int | None = None,
        statement_timeout_ms: int = 0,
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """Stream rows in batches, pulling them from the driver as they are asked for.

        ODBC has no portable per-statement timeout, so ``statement_timeout_ms``
        is ignored here; cap the read with ``limit`` instead.
        """
        sql, params = self._qb.build_stream_select(table, columns, filters, sort, limit)
        if statement_timeout_ms > 0:
            logger.debug("ODBC ignores statement_timeout_ms (%d)", statement_timeout_ms)
        async with self._pool.acquire() as conn, conn.cursor() as cur:
            await cur.execute(sql, list(params)) if params else await cur.execute(sql)
            if cur.description is None:
                return
            names = [column[0] for column in cur.description]
            while True:
                rows = await cur.fetchmany(batch_size)
                if not rows:
                    break
                yield [dict(zip(names, row, strict=False)) for row in rows]

    async def row_estimates(self, tables: list[str]) -> dict[str, int | None]:
        """Row counts from the catalog views, never a ``COUNT(*)``.

        SQL Server keeps per-partition counts in ``sys.partitions`` and Oracle
        keeps ``ALL_TABLES.NUM_ROWS`` (as of the last statistics gather, so it
        is NULL on a never-analysed table); other ODBC sources have no portable
        equivalent, so they report nothing.
        """
        if not tables:
            return {}
        estimates: dict[str, int | None] = dict.fromkeys(tables)
        if self._oracle:
            return await self._oracle_row_estimates(tables, estimates)
        if not self._mssql:
            return estimates
        placeholders = ", ".join("?" for _ in tables)
        rows, _ = await self._execute(
            "SELECT t.name AS table_name, SUM(p.rows) AS row_count "
            "FROM sys.tables t "
            "JOIN sys.schemas s ON s.schema_id = t.schema_id "
            "JOIN sys.partitions p ON p.object_id = t.object_id AND p.index_id IN (0, 1) "
            f"WHERE s.name = ? AND t.name IN ({placeholders}) "
            "GROUP BY t.name",
            [self._schema, *tables],
        )
        for row in rows:
            values = list(row.values())
            name, count = str(values[0]), values[1]
            if name in estimates and count is not None:
                estimates[name] = int(count)
        return estimates

    async def _oracle_row_estimates(
        self, tables: list[str], estimates: dict[str, int | None]
    ) -> dict[str, int | None]:
        """Fill ``estimates`` from ``ALL_TABLES.NUM_ROWS``.

        Table names are folded for the lookup and mapped back to the caller's
        spelling, so a caller asking for ``users`` gets an answer even though
        Oracle stores ``USERS``.
        """
        by_folded = {self.dialect.fold_identifier(name): name for name in tables}
        placeholders = ", ".join("?" for _ in by_folded)
        rows, _ = await self._execute(
            _ORACLE_ROW_ESTIMATES_SQL.format(placeholders=placeholders),
            [self._schema, *by_folded],
        )
        for row in rows:
            values = list(row.values())
            name, count = str(values[0]), values[1]
            original = by_folded.get(name)
            if original is not None and count is not None:
                estimates[original] = int(count)
        return estimates

    async def select_by_id(
        self, table: str, id_column: str, id_value: Any, columns: list[str] | None = None
    ) -> dict[str, Any] | None:
        """Select a single record by ID."""
        sql, params = self._qb.build_select_by_id(table, id_column, id_value, columns)
        rows, _ = await self._execute(sql, params)
        return rows[0] if rows else None

    async def update(
        self, table: str, id_column: str, id_value: Any, data: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Update a record and return it, or ``None`` when it does not exist."""
        if not data:
            return await self.select_by_id(table, id_column, id_value)

        if self._use_output(table):
            sql, values = self._qb.build_update(table, id_column, id_value, data)
            try:
                rows, _ = await self._execute(sql, values)
            except Exception as e:
                if not _is_output_with_trigger_error(e):
                    raise
                self._disable_output(table, e)
            else:
                return rows[0] if rows else None

        sql, values = self._qb.build_update(table, id_column, id_value, data, returning=False)
        _, rowcount = await self._execute(sql, values)
        if rowcount == 0:
            return None
        # rowcount > 0, or -1 when the driver does not report it: re-read.
        return await self.select_by_id(table, id_column, id_value)

    async def delete(self, table: str, id_column: str, id_value: Any) -> bool:
        """Delete a record by ID; ``True`` when a row was removed."""
        if self._use_output(table):
            sql, params = self._qb.build_delete(table, id_column, id_value)
            try:
                rows, _ = await self._execute(sql, params)
            except Exception as e:
                if not _is_output_with_trigger_error(e):
                    raise
                self._disable_output(table, e)
            else:
                return bool(rows)

        # Generic drivers may not report a rowcount: confirm existence first.
        if not self._mssql and await self.select_by_id(table, id_column, id_value) is None:
            return False
        sql, params = self._qb.build_delete(table, id_column, id_value, returning=False)
        _, rowcount = await self._execute(sql, params)
        return rowcount != 0

    # --- shared safety helpers (exercised directly by the adapter tests) --------

    def _sanitize_identifier(self, name: str) -> str:
        """Validate a SQL identifier (delegates to the shared sanitizer)."""
        return sanitize_identifier(name)

    def _build_where_clause(
        self, column: str, operator: str, value: Any, param_idx: int
    ) -> tuple[str, int, list[Any]]:
        """Build a WHERE clause component (delegates to the shared builder)."""
        return self._qb.where_clause(column, operator, value, param_idx)
